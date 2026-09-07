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
    private data class Snapshot(val pkg: String, val window: Int, val fingerprint: String, val created: Long, val nodes: Map<String, AccessibilityNodeInfo>)
    private var snapshot: Snapshot? = null
    private var revision = 0L
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var overlay: LinearLayout? = null
    private var consent: CompletableDeferred<Boolean>? = null
    private var consentId: String? = null
    private var consentTargetValid: (() -> Boolean)? = null
    companion object {
        @Volatile var current: ShuffleAccessibilityService? = null
            private set
        internal val sensitive = Regex("password|passcode|sign.?in|log.?in|permission|allow access|credential|payment|purchase|checkout|credit card|verify|verification|one.time|security|pin code|unlock|biometric|fingerprint|account number|routing number|card number|cvv|cvc|send money|transfer money|pay now", RegexOption.IGNORE_CASE)
    }
    override fun onServiceConnected() { current = this; ToolsPreferences(this).setCrossAppEnabled(false) }
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        val pendingTarget = consentTargetValid
        val captured = snapshot
        val unchanged = when {
            pendingTarget != null -> pendingTarget()
            captured != null -> {
                val root = safeRoot()
                if (root == null) false else try {
                    root.windowId == captured.window && root.packageName?.toString() == captured.pkg && fingerprint(root) == captured.fingerprint
                } finally { root.recycle() }
            }
            else -> true
        }
        if (!unchanged) { revision++; clearSnapshot(); cancelConsent() }
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

    private suspend fun approve(detail: String, deadline: Long, targetStillValid: () -> Boolean): Boolean {
        if (!enabled() || consent != null) return false
        val expires = minOf(deadline, System.currentTimeMillis() + 20_000)
        if (expires <= System.currentTimeMillis()) return false
        val id = UUID.randomUUID().toString(); val reply = CompletableDeferred<Boolean>()
        consentId = id; consent = reply; consentTargetValid = targetStillValid; removeOverlay()
        val panel = panel(); val text = label(detail)
        val proposal = android.widget.ScrollView(this).apply { addView(text) }
        panel.addView(proposal, LinearLayout.LayoutParams(-1, (resources.displayMetrics.heightPixels * 0.32).toInt()))
        val controls = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        panel.addView(controls)
        controls.addView(button("Allow once") {
            if (current === this && consentId == id && enabled() && System.currentTimeMillis() < expires && targetStillValid()) reply.complete(true)
            else reply.complete(false)
        })
        controls.addView(button("Deny") { if (consentId == id) reply.complete(false) })
        if (!show(panel)) { consent = null; consentId = null; consentTargetValid = null; return false }
        val countdown = scope.launch {
            while (isActive && !reply.isCompleted) {
                text.text = "$detail\n\nExpires in ${((expires - System.currentTimeMillis() + 999) / 1000).coerceAtLeast(0)} seconds."
                delay(200)
            }
        }
        return try {
            withTimeoutOrNull((expires - System.currentTimeMillis()).coerceAtLeast(1)) { reply.await() } == true &&
                current === this && consentId == id && enabled() && System.currentTimeMillis() < expires && targetStillValid()
        } finally { countdown.cancel(); consent = null; consentId = null; consentTargetValid = null; removeOverlay(); updateIndicator() }
    }
    private fun safeRoot(): AccessibilityNodeInfo? {
        if (!enabled() || (getSystemService(KEYGUARD_SERVICE) as KeyguardManager).isKeyguardLocked) return null
        val windowsNow = windows
        try {
            val target = windowsNow.singleOrNull { it.type == AccessibilityWindowInfo.TYPE_APPLICATION && it.isActive } ?: return null
            if (windowsNow.any { (it.isActive || it.isFocused) && it.id != target.id && it.type != AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY }) return null
            val root = target.root ?: return null
            val pkg = root.packageName?.toString()
            if (pkg !in ToolsPreferences(this).allowedApps || pkg !in AppControlPolicy.candidates(this) || unsafe(root, 0, intArrayOf(0))) { root.recycle(); return null }
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
    private fun fingerprint(root: AccessibilityNodeInfo): String {
        val out = StringBuilder()
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            if (depth > 20 || node.isPassword) return
            val bounds = android.graphics.Rect(); node.getBoundsInScreen(bounds)
            out.append(node.windowId).append('|').append(node.viewIdResourceName).append('|').append(node.className)
                .append('|').append(node.text).append('|').append(node.contentDescription).append('|').append(bounds)
                .append('|').append(node.isClickable).append('|').append(node.isScrollable).append('|').append(node.isEditable)
                .append('|').append(node.isVisibleToUser).append('|').append(node.childCount).append(';')
            for (i in 0 until node.childCount) { val child = node.getChild(i) ?: continue; visit(child, depth + 1); child.recycle() }
        }
        visit(root, 0)
        return out.toString()
    }
    suspend fun execute(name: String, args: JSONObject, deadline: Long = System.currentTimeMillis() + 20_000): JSONObject {
        if (!enabled()) return ToolRegistry.error("cross_app_disabled", "Enable Allow while using other apps and choose allowed apps in Phone tools. OS accessibility must be connected.")
        val initial = safeRoot() ?: return ToolRegistry.error("protected_screen", "This screen is not an allowed app, or contains security, permission, credential or payment content.")
        val pkg = initial.packageName.toString(); val window = initial.windowId; val fingerprint = fingerprint(initial); initial.recycle()
        val ref = if (name == "read_screen") null else ToolRegistry.text(args, "ref")
        val before = snapshot; val version = revision
        if (ref != null && !valid(before, pkg, window, fingerprint, ref)) return ToolRegistry.error("stale_ref", "Read the current screen again; this reference is stale.")
        val direction = if (name == "scroll") ToolRegistry.text(args, "direction") else ""
        require(name != "scroll" || direction in setOf("up", "down", "left", "right")) { "direction must be up, down, left or right." }
        val appName = AppControlPolicy.candidates(this)[pkg] ?: pkg
        val nodeText = before?.nodes?.get(ref)?.let { "${it.text?.toString().orEmpty()} ${it.contentDescription?.toString().orEmpty()}" }.orEmpty().take(200)
        val proposed = when (name) { "read_screen" -> "Read visible screen content and send it to your selected server"; "tap_node" -> "Tap $nodeText"; "scroll" -> "Scroll $direction on $nodeText"; "go_back" -> "Go back from this screen"; else -> return ToolRegistry.error("unsupported", "Unsupported screen action.") }
        fun unchanged(): Boolean {
            if (revision != version || System.currentTimeMillis() >= deadline) return false
            val root = safeRoot() ?: return false
            return try { root.windowId == window && root.packageName?.toString() == pkg && fingerprint(root) == fingerprint } finally { root.recycle() }
        }
        if (!approve("$appName ($pkg)\n$proposed${ref?.let { "\nReference: $it" }.orEmpty()}\nServer: ${DeviceToolsRuntime.selectedServerName().orEmpty()}", deadline, ::unchanged))
            return ToolRegistry.error("denied", "Local consent denied, expired or target changed; no action was performed.")
        if (!unchanged()) return ToolRegistry.error("stale_ref", "The screen changed or the command expired.")
        val root = safeRoot() ?: return ToolRegistry.error("protected_screen", "Screen is no longer available.")
        try {
            if (name == "read_screen") return capture(root, pkg, fingerprint)
            if (snapshot !== before || !valid(before, pkg, window, fingerprint, ref!!)) return ToolRegistry.error("stale_ref", "Screen changed while awaiting approval.")
            val node = before!!.nodes[ref]!!
            if (!node.refresh() || node.isPassword || node.isEditable || !node.isVisibleToUser || node.windowId != window || node.packageName?.toString() != pkg || System.currentTimeMillis() >= deadline)
                return ToolRegistry.error("stale_ref", "Target is no longer safe or available.")
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
        } finally { root.recycle() }
    }
    private fun valid(s: Snapshot?, pkg: String, window: Int, fingerprint: String, ref: String): Boolean = s != null && s.pkg == pkg && s.window == window && s.fingerprint == fingerprint &&
        SystemClock.elapsedRealtime() - s.created < 60_000 && s.nodes.containsKey(ref)
    private fun capture(root: AccessibilityNodeInfo, pkg: String, fingerprint: String): JSONObject {
        clearSnapshot(); val token = UUID.randomUUID().toString(); val refs = linkedMapOf<String, AccessibilityNodeInfo>(); val rows = JSONArray()
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            if (depth > 20 || rows.length() >= 80 || node.isPassword || !node.isVisibleToUser) return
            if (!node.isEditable && (node.isClickable || node.isScrollable || node.text != null || depth == 0)) {
                val ref = "$token:${refs.size}"; refs[ref] = AccessibilityNodeInfo.obtain(node)
                rows.put(JSONObject().put("ref", ref).put("text", node.text?.toString()?.take(160).orEmpty())
                    .put("description", node.contentDescription?.toString()?.take(160).orEmpty()).put("clickable", node.isClickable).put("scrollable", node.isScrollable))
            }
            for (i in 0 until node.childCount) { val child = node.getChild(i) ?: continue; visit(child, depth + 1); child.recycle() }
        }
        visit(root, 0); snapshot = Snapshot(pkg, root.windowId, fingerprint, SystemClock.elapsedRealtime(), refs)
        return JSONObject().put("ok", true).put("package", pkg).put("snapshot", token).put("nodes", rows)
    }
}
