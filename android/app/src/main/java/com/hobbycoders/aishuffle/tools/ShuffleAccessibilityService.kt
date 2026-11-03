package com.hobbycoders.aishuffle.tools

import android.accessibilityservice.AccessibilityService
import android.app.KeyguardManager
import android.graphics.Color
import android.os.SystemClock
import android.view.Gravity
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import kotlinx.coroutines.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/** Sees each node of a proving read: the node, its depth, its place in the traversal and its row. False skips collecting below it. */
private typealias NodeVisitor = (AccessibilityNodeInfo, Int, Int, ScreenRow) -> Boolean

/** OS-managed, explicitly armed per process; no boot receiver, wake word or overlay permission. */
class ShuffleAccessibilityService : AccessibilityService() {
    /** A reference is bound to one source node at one place in the tree, with the exact contents it had when captured. */
    private class Target(val node: AccessibilityNodeInfo, val ordinal: Int, val row: ScreenRow)
    private data class Snapshot(val pkg: String, val window: Int, val fingerprint: ScreenPrint, val created: Long, val nodes: Map<String, Target>)
    private class Watch(val key: Any, val read: (Boolean) -> Reading)
    private class Captured(val reading: Reading, val token: String, val refs: Map<String, Target>, val rows: JSONArray)
    /** [performed] is set only once the final checks all passed. */
    private class Attempt(val reading: Reading, val why: String, val performed: Boolean? = null)
    /** The action's final checks read this off the main thread; only the main thread writes it. */
    @Volatile private var snapshot: Snapshot? = null
    @Volatile private var revision = 0L
    /**
     * Every event delivered, with no check needed first, so a proving read can tell if one landed
     * since it started. Not filtered by package: Android keeps one pending event per type across all
     * apps, so the event that reaches us may have replaced the target's own.
     */
    @Volatile private var events = 0L
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    /** Not tied to the service: an abandoned read only ever ends by itself, and nothing awaits it. */
    private val reads = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var overlay: LinearLayout? = null
    private var consent: CompletableDeferred<Boolean>? = null
    private var consentId: String? = null
    private var consentTargetValid: ((Boolean) -> Reading)? = null
    private var sentinelCheck: Job? = null
    private var sentinelEvent: String? = null
    companion object {
        @Volatile var current: ShuffleAccessibilityService? = null
            private set
        /** Test observer between the panel closing and the final fence read; it never authorizes anything. */
        @androidx.annotation.VisibleForTesting @Volatile internal var afterConsentClosed: (suspend () -> Unit)? = null
        /** Test observer once a final attempt's result is back and before the fence sizes any retry window from it. Outside the attempt's own time limit; it never authorizes anything. */
        @androidx.annotation.VisibleForTesting @Volatile internal var beforeRetryWindow: (suspend (Reading) -> Unit)? = null
        /** Test observer off the main thread, once a final whole-screen read has finished and before the checks after it. */
        @androidx.annotation.VisibleForTesting @Volatile internal var afterFinalRead: (() -> Unit)? = null
        /** Test delay on the event fence's own check, so a test can show what holds without it. */
        @androidx.annotation.VisibleForTesting @Volatile internal var eventCheckDelayMs = 0L
        /** Test observer off the main thread, after each read by the event fence's own check: whether it was fresh, and what it found. */
        @androidx.annotation.VisibleForTesting @Volatile internal var afterEventCheckRead: ((Boolean, Reading) -> Unit)? = null
        internal val sensitive = Regex("password|passcode|sign.?in|log.?in|permission|allow access|credential|payment|purchase|checkout|credit card|verify|verification|one.time|security|pin code|unlock|biometric|fingerprint|account number|routing number|card number|cvv|cvc|send money|transfer money|pay now", RegexOption.IGNORE_CASE)
    }
    override fun onServiceConnected() { current = this; ToolsPreferences(this).setCrossAppEnabled(false) }
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        events++
        if (sentinel() == null) return
        sentinelEvent = "event=${event.eventType} ownPackage=${event.packageName == packageName}"
        if (sentinelCheck?.isActive != true) sentinelCheck = scope.launch {
            if (com.hobbycoders.aishuffle.BuildConfig.DEBUG) delay(eventCheckDelayMs)
            checkSentinel()
        }
    }
    @androidx.annotation.VisibleForTesting internal val eventsSeen get() = events
    /**
     * Off the main thread too: a cached read still calls into the target on a cache miss, which can
     * block for seconds on a hung app. Events during a check are folded into one more check.
     */
    private suspend fun checkSentinel() {
        while (true) {
            val cause = sentinelEvent ?: return
            sentinelEvent = null
            val watch = sentinel() ?: continue
            fun watched() = sentinel()?.key === watch.key
            fun checked(fresh: Boolean) = watch.read(fresh).also { if (com.hobbycoders.aishuffle.BuildConfig.DEBUG) afterEventCheckRead?.invoke(fresh, it) }
            val cached = offMain(System.currentTimeMillis() + AppControlFence.READ_MS) { checked(false) }?.verdict ?: FenceRead.UNCONFIRMED
            val holds = cached == FenceRead.SAME || (cached == FenceRead.UNCONFIRMED && AppControlFence.settle(AppControlFence.READ_MS + AppControlFence.QUIET_MS, SystemClock::elapsedRealtime) {
                if (!watched()) Reading(FenceRead.SAME) else offMain(System.currentTimeMillis() + AppControlFence.READ_MS) { checked(true) } ?: Reading(FenceRead.CHANGED)
            })
            // A verdict on something no longer vouched for must not cancel what replaced it, which gets its own check.
            if (!watched()) { if (sentinelEvent == null) sentinelEvent = cause }
            else if (!holds) invalidate(if (cached == FenceRead.CHANGED) cause else "settle")
        }
    }
    /** What this service is currently vouching for, if anything. Takes main-thread state; its read runs off the main thread. */
    private fun sentinel(): Watch? = consentTargetValid?.let { Watch(it, it) } ?: snapshot?.let { captured -> Watch(captured) { fresh ->
        val until = readUntil(Long.MAX_VALUE)
        val root = allowedRoot()
        if (root == null) Reading(FenceRead.CHANGED) else try {
            if (root.windowId == captured.window && root.packageName?.toString() == captured.pkg) compare(captured.fingerprint, root, fresh, ReadLimit(until, System::currentTimeMillis)) else Reading(FenceRead.CHANGED)
        } finally { root.recycle() }
    } }
    private fun readUntil(limit: Long) = minOf(limit, System.currentTimeMillis() + AppControlFence.READ_MS)
    /**
     * One read off the main thread, so Deny, Stop, expiry and the event fence keep running through
     * it, even if a single call into a hung target blocks for seconds. The wait ends at the read's
     * limit regardless; an abandoned read stops at its next node and nothing looks at its result.
     * Null means no answer in time, and any failure inside is a refusal too.
     */
    private suspend fun <T : Any> offMain(limit: Long, read: () -> T?): T? =
        withTimeoutOrNull(minOf(AppControlFence.READ_MS, limit - System.currentTimeMillis())) {
            reads.async { runCatching(read).onFailure { fenceTrace("read failed ${it.javaClass.simpleName}") }.getOrNull() }.await()
        }
    private fun invalidate(cause: String) {
        fenceTrace("$cause revision=$revision pending=${consentTargetValid != null} snapshot=${snapshot != null}")
        revision++; clearSnapshot(); cancelConsent()
    }
    override fun onInterrupt() { cancelConsent(); clearSnapshot(); ToolsPreferences(this).setCrossAppEnabled(false) }
    override fun onUnbind(intent: android.content.Intent?): Boolean { shutdown(); return super.onUnbind(intent) }
    override fun onDestroy() { shutdown(); scope.cancel(); super.onDestroy() }
    private fun shutdown() {
        cancelConsent(); clearSnapshot(); removeOverlay()
        if (current === this) current = null
        ToolsPreferences(this).setCrossAppEnabled(false)
        DeviceToolsRuntime.stop()
    }
    internal fun cancelConsent() { consent?.complete(false); consentId = null; if (!enabled()) removeOverlay() }
    private fun clearSnapshot() { snapshot?.nodes?.values?.forEach { it.node.recycle() }; snapshot = null }
    private fun enabled(): Boolean = current === this && ToolsPreferences(this).crossAppEnabled && ToolsPreferences(this).accessibilityEnabled
    private fun removeOverlay() {
        overlay?.let { runCatching { (getSystemService(WINDOW_SERVICE) as WindowManager).removeViewImmediate(it) } }
        overlay = null
    }
    /** Always visible while armed, including when Android notification permission is denied. */
    internal fun updateIndicator() {
        if (consent != null) { if (!enabled()) { cancelConsent(); removeOverlay() }; return }
        removeOverlay()
        if (!enabled()) return
        val server = DeviceToolsRuntime.selectedServerName() ?: return
        val panel = panel()
        panel.addView(label("Shuffle app control · $server\nEach screen action needs approval. No background microphone."))
        panel.addView(button("Stop app control") { ToolsPreferences(this).setCrossAppEnabled(false) })
        show(panel)
    }
    private fun panel() = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL; setPadding(24, 12, 24, 12); setBackgroundColor(Color.rgb(23, 43, 58))
    }
    private fun label(text: String) = TextView(this).apply { this.text = text; textSize = 15f; setTextColor(Color.WHITE) }
    private fun button(text: String, action: () -> Unit) = Button(this).apply {
        this.text = text; isAllCaps = false; guardConsentTouch(this); setOnClickListener { action() }
    }
    private fun show(panel: LinearLayout): Boolean = try {
        val params = WindowManager.LayoutParams(WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY, WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            android.graphics.PixelFormat.TRANSLUCENT).apply { gravity = Gravity.TOP; title = "Shuffle local app-control consent" }
        (getSystemService(WINDOW_SERVICE) as WindowManager).addView(panel, params); overlay = panel; true
    } catch (_: Exception) { overlay = null; false }

    /** Returns the effective expiry when approved, which is then the limit on everything that follows; null otherwise. */
    private suspend fun approve(detail: String, deadline: Long, targetStillValid: (Boolean, Long) -> Reading): Long? {
        if (!enabled() || consent != null) return null
        val expires = minOf(deadline, System.currentTimeMillis() + 20_000)
        if (expires <= System.currentTimeMillis()) return null
        val id = UUID.randomUUID().toString(); val reply = CompletableDeferred<Boolean>()
        consentId = id; consent = reply; consentTargetValid = { targetStillValid(it, expires) }; removeOverlay()
        fun pending() = current === this && consentId == id && enabled() && System.currentTimeMillis() < expires
        suspend fun confirmed() = settled(expires) {
            if (!pending()) return@settled Reading(FenceRead.CHANGED)
            val reading = offMain(expires) { targetStillValid(true, expires) } ?: Reading(FenceRead.CHANGED)
            if (pending()) reading else Reading(FenceRead.CHANGED)
        }
        val panel = panel(); val text = label(detail)
        val proposal = android.widget.ScrollView(this).apply { addView(text) }
        panel.addView(proposal, LinearLayout.LayoutParams(-1, (resources.displayMetrics.heightPixels * 0.32).toInt()))
        val controls = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        panel.addView(controls)
        controls.addView(button("Allow once") { scope.launch { reply.complete(confirmed()) } })
        controls.addView(button("Deny") { if (consentId == id) { consentId = null; reply.complete(false) } })
        if (!show(panel)) { consent = null; consentId = null; consentTargetValid = null; return null }
        val countdown = scope.launch {
            while (isActive && !reply.isCompleted) {
                text.text = "$detail\n\nExpires in ${((expires - System.currentTimeMillis() + 999) / 1000).coerceAtLeast(0)} seconds."
                delay(200)
            }
        }
        return try {
            if (withTimeoutOrNull((expires - System.currentTimeMillis()).coerceAtLeast(1)) { reply.await() } == true && confirmed()) expires else null
        } finally { countdown.cancel(); consent = null; consentId = null; consentTargetValid = null; removeOverlay(); updateIndicator() }
    }
    private fun fenceTrace(detail: String) {
        if (com.hobbycoders.aishuffle.BuildConfig.DEBUG) android.util.Log.i("AppControlFence", detail)
    }
    private fun rejectedRoot(reason: String): AccessibilityNodeInfo? { fenceTrace("root=$reason"); return null }
    /** The one active application window, provided nothing else has focus. */
    private fun <T : Any> withActiveApp(use: (AccessibilityWindowInfo) -> T?): T? {
        if (!enabled() || (getSystemService(KEYGUARD_SERVICE) as KeyguardManager).isKeyguardLocked) { fenceTrace("root=disabled_or_locked"); return null }
        val windowsNow = windows
        try {
            val target = windowsNow.singleOrNull { it.type == AccessibilityWindowInfo.TYPE_APPLICATION && it.isActive }
            if (target == null) { fenceTrace("root=active_app_count=${windowsNow.count { it.type == AccessibilityWindowInfo.TYPE_APPLICATION && it.isActive }}"); return null }
            if (windowsNow.any { (it.isActive || it.isFocused) && it.id != target.id && it.type != AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY }) { fenceTrace("root=other_focused_window"); return null }
            return use(target)
        } finally { windowsNow.forEach { it.recycle() } }
    }
    /** The active window's root, if it belongs to an allowed app. Proving reads run the safety checks on every node themselves. */
    private fun allowedRoot(): AccessibilityNodeInfo? = withActiveApp { target ->
        val root = target.root ?: return@withActiveApp rejectedRoot("missing_window_root")
        val pkg = root.packageName?.toString()
        if (pkg !in ToolsPreferences(this).allowedApps || pkg !in AppControlPolicy.candidates(this)) { root.recycle(); rejectedRoot("not_allowed") } else root
    }
    private fun safeRoot(): AccessibilityNodeInfo? = allowedRoot()?.let { root ->
        if (unsafe(root, 0, ReadLimit(readUntil(Long.MAX_VALUE), System::currentTimeMillis))) { root.recycle(); rejectedRoot("unsafe") } else root
    }
    private fun blocked(node: AccessibilityNodeInfo) = node.isPassword || // fail closed; never descend into a password subtree
        sensitive.containsMatchIn("${node.text?.toString().orEmpty()} ${node.contentDescription?.toString().orEmpty()}")
    private fun unsafe(node: AccessibilityNodeInfo, depth: Int, limit: ReadLimit): Boolean {
        if (!limit.admit(depth) || blocked(node)) return true
        return !limit.eachChild(node.childCount, { node.getChild(it) }) { child ->
            val safe = !unsafe(child, depth + 1, limit); child.recycle(); safe
        }
    }
    /** The source node's hash stands for its identity: rows that read alike from different views must not compare alike. */
    private fun rowOf(node: AccessibilityNodeInfo): ScreenRow {
        val bounds = android.graphics.Rect(); node.getBoundsInScreen(bounds)
        return ScreenRow(node.hashCode(), node.windowId, node.viewIdResourceName, node.className?.toString(), node.text?.toString(), node.contentDescription?.toString(),
            listOf(bounds.left, bounds.top, bounds.right, bounds.bottom), node.isClickable, node.isScrollable, node.isEditable, node.isVisibleToUser, node.childCount)
    }
    /**
     * With [fresh], every node is re-read past the accessibility cache, which can retain a bad read
     * until the next event. Null if [limit] ended the read, a node or an advertised child has gone,
     * or anything unsafe has appeared: none of those proved the screen.
     */
    private fun fingerprint(root: AccessibilityNodeInfo, fresh: Boolean, limit: ReadLimit, onNode: NodeVisitor? = null): ScreenPrint? {
        val rows = ArrayList<ScreenRow>(); var ordinal = 0
        fun visit(node: AccessibilityNodeInfo, depth: Int, collect: Boolean): Boolean {
            if (!limit.admit(depth) || (fresh && !node.refresh()) || blocked(node)) return false
            val row = rowOf(node)
            rows += row
            val collectChildren = collect && onNode?.invoke(node, depth, ordinal, row) == true
            ordinal++
            return limit.eachChild(node.childCount, { node.getChild(it) }) { child ->
                val proved = visit(child, depth + 1, collectChildren); child.recycle(); proved
            }
        }
        return if (visit(root, 0, true)) ScreenPrint(rows) else null
    }
    /** Debug diagnostics report only changed field names/counts, never screen values. */
    private fun fingerprintChange(expected: ScreenPrint, actual: ScreenPrint): String {
        val changed = expected.rows.zip(actual.rows).flatMap { (left, right) -> left.changedFields(right) }.toSet()
        return "rows=${expected.rows.size}->${actual.rows.size} fields=$changed"
    }
    private fun compare(expected: ScreenPrint, root: AccessibilityNodeInfo, fresh: Boolean, limit: ReadLimit, onNode: NodeVisitor? = null): Reading {
        val started = SystemClock.elapsedRealtime()
        val actual = fingerprint(root, fresh, limit, onNode)
        val readMs = SystemClock.elapsedRealtime() - started
        val verdict = if (actual == null) FenceRead.CHANGED else AppControlFence.compare(expected, actual)
        if (com.hobbycoders.aishuffle.BuildConfig.DEBUG && (fresh || verdict != FenceRead.SAME)) {
            fenceTrace("fingerprint $verdict fresh=$fresh nodes=${limit.nodes} ms=$readMs ${if (actual == null) "stopped" else fingerprintChange(expected, actual)}")
        }
        return Reading(verdict, readMs)
    }
    private suspend fun settled(limit: Long, read: suspend (until: Long) -> Reading): Boolean =
        AppControlFence.settle(limit - System.currentTimeMillis(), SystemClock::elapsedRealtime, read = read)
    /** A baseline must not be one bad read, or the exact match would later refuse a screen that never changed. */
    private suspend fun stableFingerprint(pkg: String, window: Int, deadline: Long): ScreenPrint? =
        AppControlFence.stable(deadline - System.currentTimeMillis(), SystemClock::elapsedRealtime) {
            offMain(deadline) {
                val until = readUntil(deadline)
                val root = allowedRoot()
                if (root == null) null else try {
                    if (root.windowId != window || root.packageName?.toString() != pkg) null
                    else fingerprint(root, fresh = true, ReadLimit(until, System::currentTimeMillis))
                } finally { root.recycle() }
            }
        }
    suspend fun execute(name: String, args: JSONObject, deadline: Long = System.currentTimeMillis() + 20_000): JSONObject {
        if (!enabled()) return ToolRegistry.error("cross_app_disabled", "Enable Allow while using other apps and choose allowed apps in Phone tools. OS accessibility must be connected.")
        val initial = offMain(deadline) { safeRoot()?.let { root -> (root.packageName.toString() to root.windowId).also { root.recycle() } } }
            ?: return ToolRegistry.error("protected_screen", "This screen is not an allowed app, or contains security, permission, credential or payment content.")
        val (pkg, window) = initial
        val ref = if (name == "read_screen") null else ToolRegistry.text(args, "ref")
        val before = snapshot
        val fingerprint = (if (ref == null) stableFingerprint(pkg, window, deadline) else before?.fingerprint) ?: return ToolRegistry.error(
            if (ref == null) "protected_screen" else "stale_ref", if (ref == null) "Screen is no longer available, or keeps changing." else "Read the current screen again; this reference is stale.")
        // After the baseline wait: a fence check already running on an older snapshot must not cancel this fresh read.
        val version = revision
        // Always off the main thread.
        fun read(fresh: Boolean, limit: Long, onNode: NodeVisitor? = null): Reading {
            if (revision != version || System.currentTimeMillis() >= limit) {
                fenceTrace("command=$name revision=$version->$revision expired=${System.currentTimeMillis() >= limit}")
                return Reading(FenceRead.CHANGED)
            }
            val until = readUntil(limit)
            val root = allowedRoot() ?: return Reading(FenceRead.CHANGED)
            return try {
                if (root.windowId == window && root.packageName?.toString() == pkg) compare(fingerprint, root, fresh, ReadLimit(until, System::currentTimeMillis) { revision != version }, onNode) else Reading(FenceRead.CHANGED)
            } finally { root.recycle() }
        }
        suspend fun readFresh(limit: Long, onNode: NodeVisitor? = null) = offMain(limit) { read(true, limit, onNode) } ?: Reading(FenceRead.CHANGED)
        /** What a read could not promise once it finished, checked on the main thread with nothing suspending before refs are published. */
        fun authorized(limit: Long) = revision == version && System.currentTimeMillis() < limit && withActiveApp { it.id == window } == true
        if (ref != null && !(valid(before, pkg, window, fingerprint, ref) && settled(deadline) { if (snapshot !== before) Reading(FenceRead.CHANGED) else readFresh(deadline) })) {
            fenceTrace("reference_precheck snapshot=${before != null} revision=$revision")
            return ToolRegistry.error("stale_ref", "Read the current screen again; this reference is stale.")
        }
        val direction = if (name == "scroll") ToolRegistry.text(args, "direction") else ""
        require(name != "scroll" || direction in setOf("up", "down", "left", "right")) { "direction must be up, down, left or right." }
        val appName = AppControlPolicy.candidates(this)[pkg] ?: pkg
        val nodeText = before?.nodes?.get(ref)?.node?.let { "${it.text?.toString().orEmpty()} ${it.contentDescription?.toString().orEmpty()}" }.orEmpty().take(200)
        val proposed = when (name) { "read_screen" -> "Read visible screen content and send it to your selected server"; "tap_node" -> "Tap $nodeText"; "scroll" -> "Scroll $direction on $nodeText"; "go_back" -> "Go back from this screen"; else -> return ToolRegistry.error("unsupported", "Unsupported screen action.") }
        val limit = approve("$appName ($pkg)\n$proposed${ref?.let { "\nReference: $it" }.orEmpty()}\nServer: ${DeviceToolsRuntime.selectedServerName().orEmpty()}", deadline) { fresh, expires -> read(fresh, expires) }
            ?: return ToolRegistry.error("denied", "Local consent denied, expired or target changed; no action was performed.")
        if (com.hobbycoders.aishuffle.BuildConfig.DEBUG) afterConsentClosed?.invoke()
        if (name == "read_screen") {
            var captured: Captured? = null
            val proved = settled(limit) {
                val started = events
                val result = offMain(limit) {
                    capture { read(true, limit, it) }.also { if (com.hobbycoders.aishuffle.BuildConfig.DEBUG) afterFinalRead?.invoke() }
                }
                // Events reach this thread, so none can land between this check and the publish below.
                if (result != null && result.reading.verdict == FenceRead.SAME && events != started) {
                    result.refs.values.forEach { it.node.recycle() }
                    result.reading.copy(verdict = FenceRead.PENDING)
                } else { captured = result; result?.reading ?: Reading(FenceRead.CHANGED) }
            }
            val result = captured
            if (!proved || result == null || !authorized(limit)) {
                result?.refs?.values?.forEach { it.node.recycle() }
                return ToolRegistry.error("stale_ref", "The screen changed or the command expired.")
            }
            clearSnapshot(); snapshot = Snapshot(pkg, window, fingerprint, SystemClock.elapsedRealtime(), result.refs)
            return ToolResultBounds.fit(JSONObject().put("ok", true).put("package", pkg).put("snapshot", result.token).put("nodes", result.rows))
        }
        val target = before!!.nodes[ref]!!
        if (snapshot !== before) return ToolRegistry.error("stale_ref", "Screen changed while awaiting approval.")
        // Its own copy: the snapshot's can be recycled the moment the fence invalidates it.
        val node = AccessibilityNodeInfo.obtain(target.node)
        /**
         * One attempt, on one thread with no hop before the action, so a stalled target can't hold
         * the main thread. In order: the whole screen; this reference is still the source node it was
         * bound to, in the same place; the target re-read; window, service and lock state; then, in
         * memory only, no event since this attempt started, the revision, the time and the snapshot.
         * [until] is the earlier of the expiry and any settling or quiet window this attempt is inside.
         */
        fun attempt(gate: DispatchGate, until: Long): Attempt {
            val started = events
            var bound = false
            val screen = read(true, until) { seen, _, ordinal, row ->
                if (ordinal == target.ordinal) bound = seen == node && row == target.row
                true
            }
            if (com.hobbycoders.aishuffle.BuildConfig.DEBUG) afterFinalRead?.invoke()
            if (screen.verdict != FenceRead.SAME) return Attempt(screen, "The screen changed or the command expired.")
            if (!bound || !node.refresh() || blocked(node) || node.packageName?.toString() != pkg) return Attempt(Reading(FenceRead.CHANGED), "Target is no longer safe or available.")
            val own = AppControlFence.compare(target.row, rowOf(node))
            if (own != FenceRead.SAME) return Attempt(screen.copy(verdict = own), "Target is no longer safe or available.")
            if (withActiveApp { it.id == window } != true) return Attempt(Reading(FenceRead.CHANGED), "The screen changed or the command expired.")
            if (events != started) return Attempt(screen.copy(verdict = FenceRead.PENDING), "The screen did not stay quiet long enough to prove.")
            if (revision != version || System.currentTimeMillis() >= until || snapshot !== before || !valid(before, pkg, window, fingerprint, ref!!)) return Attempt(Reading(FenceRead.CHANGED), "The screen changed or the command expired.")
            val performed = when (name) {
                "tap_node" -> node.isClickable && gate.send() && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                "scroll" -> node.isScrollable && gate.send() && node.performAction(when (direction) {
                    "up" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_UP.id
                    "down" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_DOWN.id
                    "left" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_LEFT.id
                    else -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_RIGHT.id
                })
                "go_back" -> gate.send() && performGlobalAction(GLOBAL_ACTION_BACK)
                else -> false
            }
            return Attempt(screen, "The screen changed or the command expired.", performed)
        }
        var refusal = "Screen changed while awaiting approval."
        var finished: Attempt? = null
        var sent = false
        var running = false
        val proved = try {
            settled(limit) { windowEnd ->
                val until = minOf(limit, System.currentTimeMillis() + windowEnd - SystemClock.elapsedRealtime())
                val gate = DispatchGate(windowEnd, SystemClock::elapsedRealtime)
                running = true
                val result = try { offMain(until) { attempt(gate, until) } } finally {
                    // Also on cancellation. From here this attempt can't send anything it hasn't already.
                    gate.abandon(); sent = gate.sent
                }
                running = result == null
                if (result == null) refusal = "The screen changed or the command expired."
                else { refusal = result.why; finished = result }
                val reading = result?.reading ?: Reading(FenceRead.CHANGED)
                if (com.hobbycoders.aishuffle.BuildConfig.DEBUG) beforeRetryWindow?.invoke(reading)
                reading
            }
        } finally {
            if (sent) clearSnapshot()
            if (!running) node.recycle() // A running attempt still holds it; once abandoned it is left to the garbage collector.
        }
        // Sent, but its answer missed the limit or window: it can't be called a success, or said not to have happened.
        val performed = finished?.performed
        if (sent && (!proved || performed == null)) return ToolRegistry.error("unconfirmed", "The app did not answer in time, so the action may or may not have happened. Read the screen before trying again.")
        if (!proved || performed == null) return ToolRegistry.error("stale_ref", refusal)
        clearSnapshot()
        return JSONObject().put("ok", performed).put("status", if (performed) "performed" else "unavailable")
    }
    private fun valid(s: Snapshot?, pkg: String, window: Int, fingerprint: ScreenPrint, ref: String): Boolean = s != null && s.pkg == pkg && s.window == window && s.fingerprint == fingerprint &&
        SystemClock.elapsedRealtime() - s.created < 60_000 && s.nodes.containsKey(ref)
    /** Collects refs during the read that proves the exact match, so a bad read can never drop or hide a node. Holds no refs unless it did. */
    private fun capture(read: (NodeVisitor) -> Reading): Captured {
        val token = UUID.randomUUID().toString(); val refs = linkedMapOf<String, Target>(); val rows = JSONArray()
        val reading = read { node, depth, ordinal, row ->
            if (rows.length() >= 80 || !node.isVisibleToUser) false else {
                if (!node.isEditable && (node.isClickable || node.isScrollable || node.text != null || depth == 0)) {
                    val ref = "$token:${refs.size}"; refs[ref] = Target(AccessibilityNodeInfo.obtain(node), ordinal, row)
                    rows.put(JSONObject().put("ref", ref).put("text", node.text?.toString()?.take(160).orEmpty())
                        .put("description", node.contentDescription?.toString()?.take(160).orEmpty()).put("clickable", node.isClickable).put("scrollable", node.isScrollable))
                }
                true
            }
        }
        if (reading.verdict != FenceRead.SAME) { refs.values.forEach { it.node.recycle() }; refs.clear() }
        return Captured(reading, token, refs, rows)
    }
}
