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

/** OS-managed, explicitly armed per process; no boot receiver, wake word or overlay permission. */
class ShuffleAccessibilityService : AccessibilityService() {
    private data class Snapshot(val pkg: String, val window: Int, val fingerprint: ScreenPrint, val created: Long, val nodes: Map<String, AccessibilityNodeInfo>)
    private var snapshot: Snapshot? = null
    private var revision = 0L
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var overlay: LinearLayout? = null
    private var consent: CompletableDeferred<Boolean>? = null
    private var consentId: String? = null
    private var consentTargetValid: ((Boolean) -> FenceRead)? = null
    private var sentinelCheck: Job? = null
    companion object {
        @Volatile var current: ShuffleAccessibilityService? = null
            private set
        /** Test observer between the panel closing and the final fence read; it never authorizes anything. */
        @androidx.annotation.VisibleForTesting @Volatile internal var afterConsentClosed: (suspend () -> Unit)? = null
        internal val sensitive = Regex("password|passcode|sign.?in|log.?in|permission|allow access|credential|payment|purchase|checkout|credit card|verify|verification|one.time|security|pin code|unlock|biometric|fingerprint|account number|routing number|card number|cvv|cvc|send money|transfer money|pay now", RegexOption.IGNORE_CASE)
    }
    override fun onServiceConnected() { current = this; ToolsPreferences(this).setCrossAppEnabled(false) }
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        // Cached first: this runs on every event, and an exact cached match needs no IPC.
        when (sentinelRead(fresh = false)) {
            FenceRead.SAME -> Unit
            FenceRead.CHANGED -> invalidate("event=${event.eventType} ownPackage=${event.packageName == packageName}")
            FenceRead.UNCONFIRMED -> if (sentinelCheck?.isActive != true) sentinelCheck = scope.launch {
                if (!AppControlFence.settle(AppControlFence.SETTLE_MS, SystemClock::elapsedRealtime) { sentinelRead(fresh = true) }) invalidate("settle")
            }
        }
    }
    private fun sentinelRead(fresh: Boolean): FenceRead {
        val pendingTarget = consentTargetValid
        val captured = snapshot
        return when {
            pendingTarget != null -> pendingTarget(fresh)
            captured != null -> {
                val root = safeRoot()
                if (root == null) FenceRead.CHANGED else try {
                    if (root.windowId == captured.window && root.packageName?.toString() == captured.pkg) compare(captured.fingerprint, root, fresh) else FenceRead.CHANGED
                } finally { root.recycle() }
            }
            else -> FenceRead.SAME
        }
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
    private fun clearSnapshot() { snapshot?.nodes?.values?.forEach { it.recycle() }; snapshot = null }
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

    private suspend fun approve(detail: String, deadline: Long, targetStillValid: (Boolean) -> FenceRead): Boolean {
        if (!enabled() || consent != null) return false
        val expires = minOf(deadline, System.currentTimeMillis() + 20_000)
        if (expires <= System.currentTimeMillis()) return false
        val id = UUID.randomUUID().toString(); val reply = CompletableDeferred<Boolean>()
        consentId = id; consent = reply; consentTargetValid = targetStillValid; removeOverlay()
        suspend fun confirmed() = settled(expires) {
            if (current === this && consentId == id && enabled() && System.currentTimeMillis() < expires) targetStillValid(true) else FenceRead.CHANGED
        }
        val panel = panel(); val text = label(detail)
        val proposal = android.widget.ScrollView(this).apply { addView(text) }
        panel.addView(proposal, LinearLayout.LayoutParams(-1, (resources.displayMetrics.heightPixels * 0.32).toInt()))
        val controls = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        panel.addView(controls)
        controls.addView(button("Allow once") { scope.launch { reply.complete(confirmed()) } })
        controls.addView(button("Deny") { if (consentId == id) reply.complete(false) })
        if (!show(panel)) { consent = null; consentId = null; consentTargetValid = null; return false }
        val countdown = scope.launch {
            while (isActive && !reply.isCompleted) {
                text.text = "$detail\n\nExpires in ${((expires - System.currentTimeMillis() + 999) / 1000).coerceAtLeast(0)} seconds."
                delay(200)
            }
        }
        return try {
            withTimeoutOrNull((expires - System.currentTimeMillis()).coerceAtLeast(1)) { reply.await() } == true && confirmed()
        } finally { countdown.cancel(); consent = null; consentId = null; consentTargetValid = null; removeOverlay(); updateIndicator() }
    }
    private fun fenceTrace(detail: String) {
        if (com.hobbycoders.aishuffle.BuildConfig.DEBUG) android.util.Log.i("AppControlFence", detail)
    }
    private fun rejectedRoot(reason: String): AccessibilityNodeInfo? { fenceTrace("root=$reason"); return null }
    private fun safeRoot(): AccessibilityNodeInfo? {
        if (!enabled() || (getSystemService(KEYGUARD_SERVICE) as KeyguardManager).isKeyguardLocked) return rejectedRoot("disabled_or_locked")
        val windowsNow = windows
        try {
            val target = windowsNow.singleOrNull { it.type == AccessibilityWindowInfo.TYPE_APPLICATION && it.isActive }
                ?: return rejectedRoot("active_app_count=${windowsNow.count { it.type == AccessibilityWindowInfo.TYPE_APPLICATION && it.isActive }}")
            if (windowsNow.any { (it.isActive || it.isFocused) && it.id != target.id && it.type != AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY }) return rejectedRoot("other_focused_window")
            val root = target.root ?: return rejectedRoot("missing_window_root")
            val pkg = root.packageName?.toString()
            if (pkg !in ToolsPreferences(this).allowedApps || pkg !in AppControlPolicy.candidates(this) || unsafe(root, 0, intArrayOf(0))) { root.recycle(); return rejectedRoot("not_allowed_or_unsafe") }
            return root
        } finally { windowsNow.forEach { it.recycle() } }
    }
    private fun unsafe(node: AccessibilityNodeInfo, depth: Int, count: IntArray): Boolean {
        if (++count[0] > 300 || depth > 20) return true
        if (node.isPassword) return true // fail closed; never descend into a password subtree
        if (sensitive.containsMatchIn("${node.text?.toString().orEmpty()} ${node.contentDescription?.toString().orEmpty()}")) return true
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val blocked = unsafe(child, depth + 1, count); child.recycle(); if (blocked) return true
        }
        return false
    }
    /** With [fresh], every node is re-read past the accessibility cache, which can retain a bad read until the next event. */
    private fun fingerprint(root: AccessibilityNodeInfo, fresh: Boolean, onNode: ((AccessibilityNodeInfo, Int) -> Boolean)? = null): ScreenPrint {
        val exact = StringBuilder(); val identity = StringBuilder()
        fun visit(node: AccessibilityNodeInfo, depth: Int, collect: Boolean) {
            if (depth > 20) return
            if (fresh && !node.refresh()) { exact.append("gone;"); identity.append("gone;"); return }
            if (node.isPassword) return
            val bounds = android.graphics.Rect(); node.getBoundsInScreen(bounds)
            val labels = "${node.windowId}|${node.viewIdResourceName}|${node.className}|${node.text}|${node.contentDescription}"
            val traits = "${node.isClickable}|${node.isScrollable}|${node.isEditable}"
            exact.append(labels).append('|').append(bounds).append('|').append(traits)
                .append('|').append(node.isVisibleToUser).append('|').append(node.childCount).append(';')
            identity.append(labels).append('|').append(traits).append('|').append(node.childCount).append(';')
            val collectChildren = collect && onNode?.invoke(node, depth) == true
            for (i in 0 until node.childCount) { val child = node.getChild(i) ?: continue; visit(child, depth + 1, collectChildren); child.recycle() }
        }
        visit(root, 0, true)
        return ScreenPrint(exact.toString(), identity.toString())
    }
    /** Debug diagnostics report only changed field names/counts, never screen values. */
    private fun fingerprintChange(expected: String, actual: String): String {
        val fields = listOf("window", "viewId", "class", "text", "description", "bounds", "clickable", "scrollable", "editable", "visible", "children")
        val before = expected.split(';'); val after = actual.split(';')
        val changed = before.zip(after).flatMap { (left, right) ->
            left.split('|').zip(right.split('|')).mapIndexedNotNull { index, (a, b) ->
                if (a != b) fields.getOrNull(index) ?: "structure" else null
            }
        }.toSet()
        return "rows=${before.size}->${after.size} fields=$changed"
    }
    private fun compare(expected: ScreenPrint, root: AccessibilityNodeInfo, fresh: Boolean, onNode: ((AccessibilityNodeInfo, Int) -> Boolean)? = null): FenceRead {
        val actual = fingerprint(root, fresh, onNode)
        val verdict = AppControlFence.compare(expected, actual)
        if (verdict != FenceRead.SAME && com.hobbycoders.aishuffle.BuildConfig.DEBUG) fenceTrace("fingerprint $verdict fresh=$fresh ${fingerprintChange(expected.exact, actual.exact)}")
        return verdict
    }
    private suspend fun settled(deadline: Long, read: () -> FenceRead): Boolean =
        AppControlFence.settle(minOf(AppControlFence.SETTLE_MS, deadline - System.currentTimeMillis()), SystemClock::elapsedRealtime, read = read)
    /** A baseline must not be one bad read, or the exact match would later refuse a screen that never changed. */
    private suspend fun stableFingerprint(pkg: String, window: Int, deadline: Long): ScreenPrint? {
        var print: ScreenPrint? = null
        settled(deadline) {
            val root = safeRoot() ?: return@settled FenceRead.CHANGED
            try {
                if (root.windowId != window || root.packageName?.toString() != pkg) return@settled FenceRead.CHANGED
                val next = fingerprint(root, fresh = true)
                val repeated = print?.exact == next.exact
                print = next
                if (repeated) FenceRead.SAME else FenceRead.UNCONFIRMED
            } finally { root.recycle() }
        }
        return print
    }
    suspend fun execute(name: String, args: JSONObject, deadline: Long = System.currentTimeMillis() + 20_000): JSONObject {
        if (!enabled()) return ToolRegistry.error("cross_app_disabled", "Enable Allow while using other apps and choose allowed apps in Phone tools. OS accessibility must be connected.")
        val initial = safeRoot() ?: return ToolRegistry.error("protected_screen", "This screen is not an allowed app, or contains security, permission, credential or payment content.")
        val pkg = initial.packageName.toString(); val window = initial.windowId; initial.recycle()
        val ref = if (name == "read_screen") null else ToolRegistry.text(args, "ref")
        val before = snapshot
        val fingerprint = (if (ref == null) stableFingerprint(pkg, window, deadline) else before?.fingerprint) ?: return ToolRegistry.error(
            if (ref == null) "protected_screen" else "stale_ref", if (ref == null) "Screen is no longer available." else "Read the current screen again; this reference is stale.")
        // After the baseline wait: a fence check already running on an older snapshot must not cancel this fresh read.
        val version = revision
        fun read(fresh: Boolean, onNode: ((AccessibilityNodeInfo, Int) -> Boolean)? = null): FenceRead {
            if (revision != version || System.currentTimeMillis() >= deadline) {
                fenceTrace("command=$name revision=$version->$revision expired=${System.currentTimeMillis() >= deadline}")
                return FenceRead.CHANGED
            }
            val root = safeRoot() ?: return FenceRead.CHANGED
            return try { if (root.windowId == window && root.packageName?.toString() == pkg) compare(fingerprint, root, fresh, onNode) else FenceRead.CHANGED } finally { root.recycle() }
        }
        if (ref != null && !(valid(before, pkg, window, fingerprint, ref) && settled(deadline) { if (snapshot !== before) FenceRead.CHANGED else read(fresh = true) })) {
            fenceTrace("reference_precheck snapshot=${before != null} revision=$revision")
            return ToolRegistry.error("stale_ref", "Read the current screen again; this reference is stale.")
        }
        val direction = if (name == "scroll") ToolRegistry.text(args, "direction") else ""
        require(name != "scroll" || direction in setOf("up", "down", "left", "right")) { "direction must be up, down, left or right." }
        val appName = AppControlPolicy.candidates(this)[pkg] ?: pkg
        val nodeText = before?.nodes?.get(ref)?.let { "${it.text?.toString().orEmpty()} ${it.contentDescription?.toString().orEmpty()}" }.orEmpty().take(200)
        val proposed = when (name) { "read_screen" -> "Read visible screen content and send it to your selected server"; "tap_node" -> "Tap $nodeText"; "scroll" -> "Scroll $direction on $nodeText"; "go_back" -> "Go back from this screen"; else -> return ToolRegistry.error("unsupported", "Unsupported screen action.") }
        if (!approve("$appName ($pkg)\n$proposed${ref?.let { "\nReference: $it" }.orEmpty()}\nServer: ${DeviceToolsRuntime.selectedServerName().orEmpty()}", deadline) { read(it) })
            return ToolRegistry.error("denied", "Local consent denied, expired or target changed; no action was performed.")
        if (com.hobbycoders.aishuffle.BuildConfig.DEBUG) afterConsentClosed?.invoke()
        if (name == "read_screen") {
            var screen: JSONObject? = null
            settled(deadline) { capture(pkg, window, fingerprint, { read(true, it) }) { screen = it } }
            return screen ?: ToolRegistry.error("stale_ref", "The screen changed or the command expired.")
        }
        var refusal = ""
        // The last read of this loop proves the screen and the target together, with no suspension before the action.
        val ready = settled(deadline) {
            val screen = read(fresh = true)
            if (screen != FenceRead.SAME) { refusal = "The screen changed or the command expired."; return@settled screen }
            if (snapshot !== before || !valid(before, pkg, window, fingerprint, ref!!)) { refusal = "Screen changed while awaiting approval."; return@settled FenceRead.CHANGED }
            val target = before!!.nodes[ref]!!
            refusal = "Target is no longer safe or available."
            if (!target.refresh() || target.isPassword || target.isEditable || target.windowId != window || target.packageName?.toString() != pkg) FenceRead.CHANGED
            else if (target.isVisibleToUser) FenceRead.SAME else FenceRead.UNCONFIRMED
        }
        if (!ready) return ToolRegistry.error("stale_ref", refusal)
        val node = before!!.nodes[ref]!!
        val performed = when (name) {
            "tap_node" -> node.isClickable && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            "scroll" -> node.isScrollable && node.performAction(when (direction) {
                "up" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_UP.id
                "down" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_DOWN.id
                "left" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_LEFT.id
                else -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_RIGHT.id
            })
            "go_back" -> performGlobalAction(GLOBAL_ACTION_BACK)
            else -> false
        }
        clearSnapshot()
        return JSONObject().put("ok", performed).put("status", if (performed) "performed" else "unavailable")
    }
    private fun valid(s: Snapshot?, pkg: String, window: Int, fingerprint: ScreenPrint, ref: String): Boolean = s != null && s.pkg == pkg && s.window == window && s.fingerprint == fingerprint &&
        SystemClock.elapsedRealtime() - s.created < 60_000 && s.nodes.containsKey(ref)
    /** Collects refs during the read that proves the exact match, so a bad read can never drop or hide a node. */
    private fun capture(pkg: String, window: Int, fingerprint: ScreenPrint, read: ((AccessibilityNodeInfo, Int) -> Boolean) -> FenceRead, done: (JSONObject) -> Unit): FenceRead {
        val token = UUID.randomUUID().toString(); val refs = linkedMapOf<String, AccessibilityNodeInfo>(); val rows = JSONArray()
        val verdict = read { node, depth ->
            if (rows.length() >= 80 || !node.isVisibleToUser) false else {
                if (!node.isEditable && (node.isClickable || node.isScrollable || node.text != null || depth == 0)) {
                    val ref = "$token:${refs.size}"; refs[ref] = AccessibilityNodeInfo.obtain(node)
                    rows.put(JSONObject().put("ref", ref).put("text", node.text?.toString()?.take(160).orEmpty())
                        .put("description", node.contentDescription?.toString()?.take(160).orEmpty()).put("clickable", node.isClickable).put("scrollable", node.isScrollable))
                }
                true
            }
        }
        if (verdict != FenceRead.SAME) { refs.values.forEach { it.recycle() }; return verdict }
        clearSnapshot(); snapshot = Snapshot(pkg, window, fingerprint, SystemClock.elapsedRealtime(), refs)
        done(JSONObject().put("ok", true).put("package", pkg).put("snapshot", token).put("nodes", rows))
        return verdict
    }
}
