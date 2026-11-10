package com.hobbycoders.aishuffle

import android.app.UiAutomation
import android.content.ComponentName
import android.content.Intent
import android.graphics.Rect
import android.provider.Settings
import android.view.accessibility.AccessibilityNodeInfo
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
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

/**
 * The approval fence on real accessibility windows, with a target the panel really covers.
 *
 * A "blip" is a genuine move that undoes itself: it gives the fence the same shape as Android's
 * one bad read (bounds differ, then match again), on demand. Blips must survive; a move that lasts
 * or any content change must be refused, under the panel and after it has gone. So must a screen
 * that reads the same from different views, a view whose fields differ but would concatenate alike,
 * a view advertising a child it can't return, and a screen too slow to read in time. An event landing after
 * the final read must force another read, and a target stalling after it must not hold the main thread.
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
    private val requests = java.util.concurrent.CopyOnWriteArrayList<Pair<CountDownLatch, AtomicReference<JSONObject>>>()

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
        assertTrue(inject(bounds))
        return true
    }
    private fun panelButton(text: String) = automation.windows.asSequence()
        .filter { it.type == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY }
        .mapNotNull { it.root }.filter { it.packageName?.toString() == context.packageName }
        .flatMap { it.findAccessibilityNodeInfosByText(text).asSequence() }
        .firstOrNull { it.text?.toString() == text && it.isClickable }
    /**
     * The fence may remove this panel between lookup and action. Send only to its
     * captured window/node: a screen-coordinate touch could hit the target below.
     * Ordinary approvals still use real input in allow(); this optional action is
     * only for cases where the fence is expected to refuse with or without it.
     */
    private fun touchIfStillUp(text: String, beforeAction: () -> Unit = {}) {
        val target = panelButton(text) ?: return
        try {
            beforeAction()
            target.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        } finally { target.recycle() }
    }
    private fun inject(bounds: Rect): Boolean {
        val now = android.os.SystemClock.uptimeMillis()
        val down = android.view.MotionEvent.obtain(now, now, android.view.MotionEvent.ACTION_DOWN, bounds.exactCenterX(), bounds.exactCenterY(), 0)
        val up = android.view.MotionEvent.obtain(now, now + 50, android.view.MotionEvent.ACTION_UP, bounds.exactCenterX(), bounds.exactCenterY(), 0)
        down.source = android.view.InputDevice.SOURCE_TOUCHSCREEN; up.source = android.view.InputDevice.SOURCE_TOUCHSCREEN
        val injected = automation.injectInputEvent(down, true) && automation.injectInputEvent(up, true)
        down.recycle(); up.recycle()
        return injected
    }
    /** What the app itself says about app control, for a failure that would otherwise only say a panel is missing. */
    private fun fenceState(): String {
        val prefs = ToolsPreferences(context)
        fun overlays(windows: () -> List<AccessibilityWindowInfo>?) = runCatching {
            windows().orEmpty().filter { it.type == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY }.map { it.id }.toString()
        }.getOrElse { it.javaClass.simpleName }
        val showing = runCatching { ShuffleAccessibilityService.current?.consentShowing() == true }.getOrDefault(false)
        return "service=${ShuffleAccessibilityService.current != null} appControl=${prefs.crossAppEnabled} accessibility=${prefs.accessibilityEnabled} " +
            "availability=${DeviceToolsRuntime.availability.value} lastDisarm=${ToolsPreferences.lastDisarm} panelShowing=$showing " +
            "overlaysListedToTest=${overlays { automation.windows }} overlaysListedToService=${overlays { ShuffleAccessibilityService.current?.windows }} " +
            "requests=${requests.map { it.second.get()?.toString() ?: if (it.first.count == 0L) "ended" else "pending" }}"
    }
    private fun panelUp() {
        var refreshAt = System.currentTimeMillis() + 1_000
        try {
            await("Consent panel never appeared") {
                if (panelButton("Allow once") != null) return@await true
                // Nothing left that could still raise a panel: say why now, not after the full wait.
                if (requests.isNotEmpty() && requests.all { it.first.count == 0L }) throw AssertionError("Every request ended without a consent panel")
                if (System.currentTimeMillis() > refreshAt) {
                    // UiAutomation answers from a cached window list until an event tells it the windows changed, and a list
                    // fetched just before the panel was added can be cached after that event. Setting its service info
                    // drops the cache, so the next lookup asks Android again. Well inside the shortest command deadline (4 s).
                    refreshAt = System.currentTimeMillis() + 1_000
                    android.util.Log.w("CrossAppFenceTest", "No consent panel listed, asking Android for its windows again: ${fenceState()}")
                    automation.serviceInfo = automation.serviceInfo
                }
                false
            }
        } catch (failure: AssertionError) { throw AssertionError("${failure.message}: ${fenceState()}", failure) }
    }
    private fun allow() { panelUp(); assertTrue(touch("Allow once")) }
    /**
     * Android can reconnect the service while a test is still setting up, and a fresh connection
     * always starts with app control off. Only that is opted in again; anything else that ended it
     * fails here, by name, instead of later as a panel that never appears.
     */
    private fun ensureArmed(stage: String) {
        repeat(3) {
            if (ToolsPreferences(context).crossAppEnabled) return
            val cause = ToolsPreferences.lastDisarm.orEmpty()
            if (listOf("onServiceConnected", "shutdown", "service=false").none { it in cause }) throw AssertionError("App control was off $stage: ${fenceState()}")
            android.util.Log.w("CrossAppFenceTest", "Opting in again $stage: ${fenceState()}")
            await("OS never reconnected ShuffleAccessibilityService") { ShuffleAccessibilityService.current != null }
            instrumentation.runOnMainSync { ToolsPreferences(context).setCrossAppEnabled(true) }
        }
        assertTrue("App control would not stay on $stage: ${fenceState()}", ToolsPreferences(context).crossAppEnabled)
    }
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
    /** Runs [steps] off the main thread, one after each final whole-screen read in turn, and says how many such reads there were. */
    private fun afterFinalReads(vararg steps: () -> Unit): AtomicInteger {
        val reads = AtomicInteger()
        ShuffleAccessibilityService.afterFinalRead = { steps.getOrNull(reads.getAndIncrement())?.invoke() }
        return reads
    }
    private fun afterFinalRead(step: () -> Unit) = afterFinalReads(step)
    /** The events from a move and the panel closing have all arrived by then, so the next read has none land in it. */
    private fun undoMoveAndLetEventsArrive() { fixture("back"); Thread.sleep(300) }
    /** Applies [command] and returns once an accessibility event has reached the service. Stops the event fence's own check getting there first. */
    private fun fixtureWithEvent(command: String) {
        val service = requireNotNull(ShuffleAccessibilityService.current)
        val seen = service.eventsSeen
        fixture(command)
        await("No event reached the service after $command") { service.eventsSeen > seen }
    }
    private fun holdEventChecks() { ShuffleAccessibilityService.eventCheckDelayMs = 1_500 }
    /** How long the app's main thread took to run something posted now. */
    private fun mainThreadDelay(): Long {
        val ran = CountDownLatch(1); val started = android.os.SystemClock.uptimeMillis()
        android.os.Handler(android.os.Looper.getMainLooper()).post { ran.countDown() }
        assertTrue("Main thread did not respond", ran.await(10, TimeUnit.SECONDS))
        return android.os.SystemClock.uptimeMillis() - started
    }
    private fun assertStopIsResponsive() {
        val held = mainThreadDelay()
        assertTrue("Main thread was held for $held ms", held < 1_000)
        val started = System.currentTimeMillis()
        assertTrue(touch("Stop app control"))
        await("Stop did not take effect", 2_000) { !ToolsPreferences(context).crossAppEnabled }
        assertTrue("Stop took ${System.currentTimeMillis() - started} ms", System.currentTimeMillis() - started < 2_000)
    }
    private fun shows(text: String) = automation.rootInActiveWindow?.findAccessibilityNodeInfosByText(text)?.any { it.text?.toString() == text } == true
    private fun request(name: String, args: JSONObject = JSONObject(), deadlineMs: Long = 20_000): Pair<CountDownLatch, AtomicReference<JSONObject>> {
        val result = AtomicReference<JSONObject>(); val done = CountDownLatch(1)
        CoroutineScope(Dispatchers.Main).launch {
            try { result.set(DeviceToolExecutor(context).execute(name, args, System.currentTimeMillis() + deadlineMs)) } finally { done.countDown() }
        }
        return (done to result).also { requests += it }
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
        ensureArmed("after opting in")
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
        ensureArmed("once the fixture was ready")
    }
    @After fun disarm() {
        ShuffleAccessibilityService.afterConsentClosed = null
        ShuffleAccessibilityService.afterFinalRead = null
        ShuffleAccessibilityService.beforeRetryWindow = null
        ShuffleAccessibilityService.eventCheckDelayMs = 0
        ShuffleAccessibilityService.afterEventCheckRead = null
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
        touchIfStillUp("Allow once")
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
        touchIfStillUp("Allow once")
        assertEquals("denied", status(tapping).getString("status"))
        assertNotTapped()
    }
    @Test fun approvalClosingBetweenLookupAndActionCannotTapTheTarget() {
        val tapping = tap(read("Track One"))
        panelUp()
        var captured = false
        touchIfStillUp("Allow once") {
            captured = true
            // Force the exact lookup -> close -> action ordering, without a sleep.
            fixture("swap")
            assertEquals("denied", status(tapping).getString("status"))
            await("Denied panel did not close") { panelButton("Allow once") == null }
        }
        assertTrue("The test must capture the approval node before closing it", captured)
        assertNotTapped()
        // Positive control: keep detecting a real action on a swapped row. The
        // helper fix must not turn the existing no-target-tap assertion into a pass.
        val swapped = requireNotNull(button("Track One"))
        try { assertTrue(swapped.performAction(AccessibilityNodeInfo.ACTION_CLICK)) }
        finally { swapped.recycle() }
        await("Fixture did not record the control tap") { shows("Tapped: Track One") }
        assertThrows(AssertionError::class.java) { assertNotTapped() }
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
    /** The first track reads text `A|B`, description `C`; "rebind" makes it `A`, `B|C`, on the same node with nothing else different. */
    private fun collidingRef(): String {
        fixture("collide")
        return read("A|B")
    }
    @Test fun sameNodeRebindAsThePanelClosesIsRefused() {
        val ref = collidingRef()
        onceClosed("rebind")
        val tapping = tap(ref); allow()
        assertEquals("stale_ref", status(tapping).getString("status"))
        assertNotTapped()
    }
    @Test fun sameNodeRebindUnderThePanelIsRefused() {
        val tapping = tap(collidingRef())
        panelUp(); fixture("rebind")
        touchIfStillUp("Allow once")
        assertEquals("denied", status(tapping).getString("status"))
        assertNotTapped()
    }
    @Test fun sameNodeRebindBeforeTheNextCommandStaleTheReference() {
        val ref = collidingRef()
        fixture("rebind")
        assertEquals("stale_ref", status(tap(ref)).getString("status"))
        assertNotTapped()
    }
    @Test fun sameNodeRebindAsItsReadPanelClosesPublishesNoRefs() {
        fixture("collide")
        onceClosed("rebind")
        val reading = request("read_screen"); allow()
        assertEquals("stale_ref", status(reading).getString("status"))
    }
    @Test fun readRefusedOnAChildItCannotGet() {
        fixture("ghost:1")
        assertEquals("protected_screen", status(request("read_screen")).getString("status"))
    }
    @Test fun readRefusedOnAParentAdvertisingThousandsOfMissingChildren() {
        fixture("ghost:1000")
        assertEquals("protected_screen", status(request("read_screen")).getString("status"))
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
        fixture("dense:100"); fixture("delay:5") // Each read takes over 500 ms; a clean one must still count.
        val tapping = tap(read(waitSeconds = 15)); allow()
        assertEquals("performed", status(tapping, 15).getString("status"))
        assertTapped(1)
    }
    /** A screen where every read takes over 500 ms, and a reference to its target. */
    private fun denseSlowRef(): String {
        fixture("dense:100"); fixture("delay:5")
        return read(waitSeconds = 15)
    }
    @Test fun denseSlowScreenTapRecoversFromABadRead() {
        val ref = denseSlowRef()
        holdEventChecks()
        onceClosed("move") // The first final read isn't exact, so the retry that proves it must fit its window.
        afterFinalRead { undoMoveAndLetEventsArrive() }
        val tapping = tap(ref); allow()
        assertEquals("performed", status(tapping, 15).getString("status"))
        assertTapped(1)
    }
    /** Returns once the event fence has finished a read of the whole cached tree this fast. The read that follows it is not. */
    private fun fastEventCheck(underMs: Long) {
        val fast = java.util.concurrent.atomic.AtomicBoolean()
        val finished = AtomicInteger()
        ShuffleAccessibilityService.afterEventCheckRead = { fresh, reading ->
            if (!fresh) { fast.set(reading.verdict == FenceRead.SAME && (reading.readMs ?: Long.MAX_VALUE) < underMs); finished.incrementAndGet() }
        }
        await("The event fence never finished a fast cached read", 12_000) {
            val seen = finished.get()
            fixture("notify")
            await("The event fence never read after an event", 5_000) { finished.get() > seen }
            fast.get()
        }
    }
    @Test fun fastEventCheckBetweenASlowBadReadAndItsRetryWindowDoesNotShrinkIt() {
        val ref = denseSlowRef()
        holdEventChecks()
        onceClosed("move")
        val attempts = AtomicInteger(); val bad = AtomicInteger(); val failure = AtomicReference<Throwable?>()
        // Not in afterFinalRead: that runs inside the attempt's own 2 s limit, and overrunning it is a refusal of its own.
        ShuffleAccessibilityService.beforeRetryWindow = { reading ->
            attempts.incrementAndGet()
            if (reading.verdict == FenceRead.UNCONFIRMED && bad.getAndIncrement() == 0) withContext(Dispatchers.IO) {
                ShuffleAccessibilityService.eventCheckDelayMs = 0
                runCatching { fixture("back"); fastEventCheck(underMs = 100) }.onFailure(failure::set) // Over 500 ms of row delays is not read in 100.
            }
        }
        val tapping = tap(ref); allow()
        val outcome = status(tapping, 25).getString("status")
        assertNull("The fast event check never happened", failure.get())
        assertTrue("The first read must have been the slow bad one", bad.get() >= 1)
        assertEquals("performed", outcome)
        assertTrue("The bad read must have been retried", attempts.get() >= 2)
        assertTapped(1)
    }
    @Test fun denseSlowScreenReadRecoversFromABadRead() {
        fixture("dense:100"); fixture("delay:5")
        holdEventChecks()
        onceClosed("move")
        afterFinalRead { undoMoveAndLetEventsArrive() }
        read(waitSeconds = 15) // Asserts ok, and that the target is still among the captured nodes.
    }
    @Test fun denseSlowScreenKeepsItsPanelThroughABlip() {
        val tapping = tap(denseSlowRef())
        panelUp()
        // The move lasts until a fresh read by the event fence has seen it, so this can't pass with nothing to recover from.
        // Undone before that read's verdict is acted on, so the retry reads only the reverted screen.
        val mismatched = CountDownLatch(1); val recovered = CountDownLatch(1)
        ShuffleAccessibilityService.afterEventCheckRead = { fresh, reading ->
            if (fresh && reading.verdict == FenceRead.UNCONFIRMED && mismatched.count == 1L) { runCatching { fixture("back") }; mismatched.countDown() }
            else if (fresh && reading.verdict == FenceRead.SAME && mismatched.count == 0L) recovered.countDown()
        }
        fixtureWithEvent("move")
        assertTrue("The event fence never read the move", mismatched.await(10, TimeUnit.SECONDS))
        assertTrue("The event fence never read the screen exact again", recovered.await(10, TimeUnit.SECONDS))
        allow()
        assertEquals("performed", status(tapping, 15).getString("status"))
        assertTapped(1)
    }
    @Test fun denseSlowScreenLastingMoveAfterThePanelClosesIsRefused() {
        val ref = denseSlowRef()
        onceClosed("move")
        val tapping = tap(ref); allow()
        assertEquals("stale_ref", status(tapping, 15).getString("status"))
        fixture("delay:0")
        assertNotTapped()
    }
    @Test fun denseSlowScreenLastingMoveUnderThePanelCancelsIt() {
        val tapping = tap(denseSlowRef())
        panelUp()
        fixtureWithEvent("move")
        assertEquals("denied", status(tapping, 15).getString("status"))
        fixture("delay:0")
        assertNotTapped()
    }
    @Test fun readTooLargeToDeliverIsCutDownAndItsReferencesStillWork() {
        fixture("dense:100"); fixture("longrows")
        val reading = request("read_screen"); allow()
        val screen = status(reading, 15)
        assertTrue(screen.toString(), screen.optBoolean("ok"))
        assertTrue("A result this large must be cut down for delivery", screen.optBoolean("truncated"))
        val nodes = screen.getJSONArray("nodes")
        val ref = (0 until nodes.length()).map { nodes.getJSONObject(it) }.single { it.optString("text") == "Covered target" }.getString("ref")
        val tapping = tap(ref); allow()
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
        touchIfStillUp("Allow once")
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
        touchIfStillUp("Allow once")
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
    @Test fun eventAfterTheFinalReadWithOtherContentChangedRefusesTheTap() {
        val ref = read()
        holdEventChecks()
        val reads = afterFinalRead { fixtureWithEvent("text") }
        val tapping = tap(ref); allow()
        assertEquals("stale_ref", status(tapping).getString("status"))
        assertTrue("A second read must have refused it", reads.get() >= 2)
        assertTrue("The target must not have been tapped", shows("Different content"))
    }
    @Test fun eventAfterTheFinalReadWithNothingChangedStillTaps() {
        val ref = read()
        holdEventChecks()
        val reads = afterFinalRead { fixtureWithEvent("notify") }
        val tapping = tap(ref); allow()
        assertEquals("performed", status(tapping).getString("status"))
        assertTrue("The event must have forced a second read", reads.get() >= 2)
        assertTapped(1)
    }
    @Test fun eventAfterTheFinalReadWithOtherContentChangedPublishesNoRefs() {
        holdEventChecks()
        afterFinalRead { fixtureWithEvent("text") }
        val reading = request("read_screen"); allow()
        assertEquals("stale_ref", status(reading).getString("status"))
    }
    @Test fun eventAfterTheFinalReadWithNothingChangedStillReads() {
        holdEventChecks()
        val reads = afterFinalRead { fixtureWithEvent("notify") }
        read()
        assertTrue("The event must have forced a second read", reads.get() >= 2)
    }
    @Test fun screenFiringEventsWithNothingChangedIsStillApproved() {
        fixture("noisy:150")
        val tapping = tap(read()); allow()
        assertEquals("performed", status(tapping).getString("status"))
        assertTapped(1)
    }
    @Test fun screenTooBusyForAnyReadToFinishQuietlyIsRefusedAtItsLimit() {
        fixture("dense:100")
        val ref = read(waitSeconds = 15)
        fixture("delay:5"); fixture("noisy:120") // Each read takes over 500 ms, and an event lands in every one.
        val closed = AtomicLong()
        ShuffleAccessibilityService.afterConsentClosed = { closed.set(System.currentTimeMillis()) }
        val tapping = tap(ref); allow()
        assertEquals("stale_ref", status(tapping, 15).getString("status"))
        // From the panel closing: the read that opens the window, the window, and one read that started inside it.
        val took = System.currentTimeMillis() - closed.get()
        assertTrue("Took $took ms", took < 2 * AppControlFence.READ_MS + AppControlFence.QUIET_MS)
        fixture("noisy:0"); fixture("delay:0")
        assertNotTapped()
    }
    @Test fun targetStallingAfterTheFinalReadLeavesStopResponsive() {
        val ref = read()
        val stalled = CountDownLatch(1)
        afterFinalRead { fixture("stallread:3500"); stalled.countDown() }
        val tapping = tap(ref); allow()
        assertTrue(stalled.await(5, TimeUnit.SECONDS)); Thread.sleep(200)
        assertStopIsResponsive()
        assertEquals("stale_ref", status(tapping).getString("status"))
        Thread.sleep(3_500)
        assertNotTapped()
    }
    @Test fun targetAnsweringAfterTheWaitEndedIsNeverTapped() {
        val ref = read()
        afterFinalRead { fixture("stallread:3500") } // Longer than one attempt's 2 s; nothing else refuses it, so only the abandoned attempt can.
        val tapping = tap(ref); allow()
        val started = System.currentTimeMillis()
        assertEquals("stale_ref", status(tapping).getString("status"))
        assertTrue("Took ${System.currentTimeMillis() - started} ms", System.currentTimeMillis() - started < 3_000)
        Thread.sleep(3_500)
        assertNotTapped()
    }
    @Test fun targetStallingOnTheTapLeavesStopResponsiveAndIsNotASuccess() {
        val ref = read()
        val stalled = CountDownLatch(1)
        afterFinalRead { fixture("stallaction:3500"); stalled.countDown() }
        val tapping = tap(ref); allow()
        assertTrue(stalled.await(5, TimeUnit.SECONDS)); Thread.sleep(200)
        assertStopIsResponsive()
        val result = status(tapping)
        assertEquals("unconfirmed", result.getString("status"))
        assertFalse(result.optBoolean("ok"))
        assertTapped(1) // A sent action can't be recalled, which is why this isn't reported as not having happened.
    }
    @Test fun exactReadCrossingTheSettlingWindowIsRefusedWithNoTap() {
        val ref = read()
        holdEventChecks()
        onceClosed("move") // The first final read sees the move, which opens the 500 ms window.
        afterFinalRead { undoMoveAndLetEventsArrive(); fixture("stallread:1200") } // The next read is exact, and under the 2 s read limit.
        val tapping = tap(ref); allow()
        assertEquals("stale_ref", status(tapping).getString("status"))
        Thread.sleep(1_500)
        assertNotTapped()
    }
    @Test fun exactReadCrossingTheQuietWindowIsRefusedWithNoTap() {
        val ref = read()
        holdEventChecks()
        afterFinalReads(
            { fixtureWithEvent("notify") }, // Opens the 2.5 s window.
            { fixtureWithEvent("notify"); fixture("stallread:1500") }, // Takes up most of it, under the 2 s read limit, and proves nothing.
            { fixture("stallread:1500") }, // Quiet and exact, also under the read limit, but its re-read of the target ends after the window.
        )
        val tapping = tap(ref); allow()
        assertEquals("stale_ref", status(tapping).getString("status"))
        Thread.sleep(2_000)
        assertNotTapped()
    }
    @Test fun actionAnsweringPastTheSettlingWindowIsNotASuccess() {
        val ref = read()
        holdEventChecks()
        onceClosed("move")
        afterFinalRead { undoMoveAndLetEventsArrive(); fixture("stallaction:1200") } // Sent inside the window, under the 2 s read limit, answered after it.
        val tapping = tap(ref); allow()
        val result = status(tapping)
        assertEquals("unconfirmed", result.getString("status"))
        assertFalse(result.optBoolean("ok"))
        assertTapped(1)
    }
}
