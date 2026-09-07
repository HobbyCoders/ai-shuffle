package com.hobbycoders.aishuffle.tools

import android.app.Activity
import android.app.Application
import android.content.Context
import android.content.Intent
import android.os.Bundle
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.voice.VoiceCallService
import com.hobbycoders.aishuffle.voice.VoiceCallState
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.lang.ref.WeakReference

/** Foreground by default; only an explicitly armed connected AccessibilityService can host cross-app polling. */
object DeviceToolsRuntime {
    private var activity = WeakReference<Activity>(null)
    private var server: Server? = null
    private var context: Context? = null
    private var registered = false
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var polling: Job? = null
    private var retiring: Job? = null
    private var unavailable: Job? = null
    private var generation = 0L
    private val mutableAvailability = MutableStateFlow("foreground_required")
    val availability: StateFlow<String> = mutableAvailability

    /** Call onResume (or after account/server selection); detach onPause. Main thread only. */
    fun attach(activity: Activity, server: Server) {
        check(android.os.Looper.myLooper() == android.os.Looper.getMainLooper())
        if (!authorizedTarget(activity, server)) { stop(); mutableAvailability.value = "authorization_revoked"; return }
        if (this.activity.get() === activity && this.server == server && polling?.isActive == true) return
        val previous = this.server
        val store = com.hobbycoders.aishuffle.data.ServerStore.get(activity)
        if (store.selectedToolsServerId != null && store.selectedToolsServerId != server.id) ToolsPreferences(activity).setCrossAppEnabled(false)
        deactivate()
        if (previous != null && previous != server && VoiceCallState.isActive && VoiceCallState.state.value.serverId != server.id) VoiceCallService.hangup(activity)
        context = activity.applicationContext
        com.hobbycoders.aishuffle.data.ServerStore.get(activity).selectedToolsServerId = server.id
        if (!registered) {
            activity.application.registerActivityLifecycleCallbacks(object : Application.ActivityLifecycleCallbacks {
                override fun onActivityPaused(a: Activity) { detach(a) }
                override fun onActivityDestroyed(a: Activity) { detach(a) }
                override fun onActivityCreated(a: Activity, b: Bundle?) {}
                override fun onActivityStarted(a: Activity) {}
                override fun onActivityResumed(a: Activity) {}
                override fun onActivityStopped(a: Activity) {}
                override fun onActivitySaveInstanceState(a: Activity, b: Bundle) {}
            })
            registered = true
        }
        this.activity = WeakReference(activity)
        this.server = server
        ToolApprovals.attach(activity)
        startPolling()
    }
    fun detach(activity: Activity) {
        if (this.activity.get() !== activity) return
        ToolApprovals.detach(activity)
        this.activity.clear()
        if (hasCrossAppHost()) {
            mutableAvailability.value = "cross_app_consent_required"
            ShuffleAccessibilityService.current?.updateIndicator()
        } else { deactivate(); server = null; mutableAvailability.value = "foreground_required" }
    }
    fun stop() {
        context?.let { ToolsPreferences(it).setCrossAppEnabled(false) }
        deactivate()
        activity.get()?.let { ToolApprovals.detach(it) }
        activity.clear()
        server = null
        context?.let { if (VoiceCallState.isActive) VoiceCallService.hangup(it) }
        VoiceCallState.reset()
        mutableAvailability.value = "signed_out"
    }
    internal fun preferencesChanged() {
        deactivate()
        val app = context ?: ShuffleAccessibilityService.current?.applicationContext ?: return
        context = app
        server = if (ToolsPreferences(app).enabled) runCatching { com.hobbycoders.aishuffle.data.ServerStore.get(app).selectedToolsServer() }.getOrNull() else null
        if ((activity.get() != null || hasCrossAppHost()) && server != null) startPolling()
        ShuffleAccessibilityService.current?.updateIndicator()
    }
    internal fun hasCrossAppHost(): Boolean = context?.let { ToolsPreferences(it).crossAppEnabled && ToolsPreferences(it).accessibilityEnabled && ShuffleAccessibilityService.current != null } == true
    internal fun selectedServerName(): String? = server?.name
    internal fun hasForegroundActivity(): Boolean = activity.get()?.let { !it.isFinishing && !it.isDestroyed && it.hasWindowFocus() } == true
    internal suspend fun launch(intent: Intent, deadline: Long): JSONObject {
        val host = activity.get() ?: return ToolRegistry.error("foreground_required", "Open AI Shuffle and retry; no action was performed.")
        val target = server ?: return ToolRegistry.error("authorization_revoked", "Reconnect the selected server; no action was performed.")
        val epoch = generation
        while (true) {
            if (System.currentTimeMillis() >= deadline) return ToolRegistry.error("expired", "Command expired; no action was performed.")
            if (activity.get() !== host || generation != epoch || server != target || host.isFinishing || host.isDestroyed)
                return ToolRegistry.error("foreground_required", "Open AI Shuffle and retry; no action was performed.")
            if (!ToolsPreferences(host).enabled) return ToolRegistry.error("disabled", "Android tools were disabled.")
            if (!authorizedTarget(host, target)) return ToolRegistry.error("authorization_revoked", "Reconnect the selected server; no action was performed.")
            if (host.hasWindowFocus()) break
            delay(16)
        }
        if (System.currentTimeMillis() >= deadline) return ToolRegistry.error("expired", "Command expired; no action was performed.")
        host.startActivity(intent)
        return JSONObject().put("ok", true).put("status", "launched_confirmation")
            .put("message", "Opened Android UI. Completion/save/send is controlled by the user and has not been verified.")
    }
    private fun deactivate() {
        generation++
        polling?.let { it.cancel(); retiring = it }
        polling = null
        unavailable?.cancel()
        ToolApprovals.cancelAll()
        ShuffleAccessibilityService.current?.cancelConsent()
        val old = server
        if (old != null) unavailable = scope.launch {
            try { DeviceToolsApi.request(old, "capabilities", capabilities(false)) }
            catch (e: CancellationException) { throw e } catch (_: Exception) { /* Offline lease expires server-side. */ }
        }
    }
    private fun capabilities(enabled: Boolean) = JSONObject().put("enabled", enabled)
        .put("tools", JSONArray(if (enabled) ToolRegistry.queuedNames.toList() else emptyList<String>()))

    private fun authorizedTarget(app: Context, target: Server): Boolean = runCatching {
        val saved = com.hobbycoders.aishuffle.data.ServerStore.get(app).getById(target.id)
        if (saved != target) return@runCatching false
        if (target.accountId == null) return@runCatching true
        val session = com.hobbycoders.aishuffle.account.AccountStore(app).session()
        session != null && session.accountId == target.accountId && session.hub == target.hubUrl
    }.getOrDefault(false)

    private fun startPolling() {
        val app = context ?: return
        val target = server ?: return
        val currentGeneration = generation
        val scopeId = "${target.id}|${target.baseUrl}|${target.deviceId}"
        unavailable?.cancel()
        val previousLoop = retiring
        polling = scope.launch {
            withContext(NonCancellable) { previousLoop?.join() }
            ensureActive()
            val journal = try { withContext(Dispatchers.IO) { CommandJournal(app) } }
                catch (e: CancellationException) { throw e }
                catch (_: Exception) { mutableAvailability.value = "secure_storage_unavailable"; return@launch }
            val executor = DeviceToolExecutor(app)
            while (isActive && generation == currentGeneration && (activity.get() != null || hasCrossAppHost())) {
                try {
                    if (!authorizedTarget(app, target)) { stop(); mutableAvailability.value = "authorization_revoked"; return@launch }
                    val enabled = ToolsPreferences(app).enabled
                    DeviceToolsApi.request(target, "capabilities", capabilities(enabled))
                    mutableAvailability.value = if (!enabled) "disabled" else if (activity.get() != null) "foreground_only" else "cross_app_consent_required"
                    // Deliver durable results first; a network failure never repeats execution.
                    for ((key, entry) in withContext(Dispatchers.IO) { journal.pending(scopeId, System.currentTimeMillis()) }) {
                        val response = DeviceToolsApi.request(target, "${entry.getString("id")}/result", JSONObject().put("result", entry.getJSONObject("result")))
                        check(response.optBoolean("ok"))
                        withContext(Dispatchers.IO) { journal.posted(key) }
                    }
                    val commands = DeviceToolsApi.request(target, "pending").optJSONArray("commands") ?: JSONArray()
                    for (i in 0 until minOf(commands.length(), 32)) {
                        ensureActive()
                        if (generation != currentGeneration) break
                        val command = commands.optJSONObject(i) ?: continue
                        val id = command.optString("id")
                        if (!id.matches(Regex("[A-Za-z0-9_-]{1,128}"))) continue
                        val key = CommandJournal.key(scopeId, id)
                        if (withContext(Dispatchers.IO) { journal.get(key) } != null) continue
                        val now = System.currentTimeMillis()
                        val expires = CommandJournal.expiry(command.opt("expires_at"))
                        // Claim even expired/unsupported/disabled requests so they receive one factual result.
                        val claimed = withContext(Dispatchers.IO) { journal.claim(key, id, scopeId, expires.coerceAtLeast(now), now) }
                        if (!claimed) { mutableAvailability.value = "queue_full"; continue }
                        val result = when {
                            !CommandJournal.validExpiry(expires, now) -> ToolRegistry.error("expired", "Command expired or has an invalid expiry (maximum lifetime: five minutes).")
                            !enabled -> ToolRegistry.error("disabled", "Android tools are disabled.")
                            command.optJSONObject("arguments") == null -> ToolRegistry.error("invalid_arguments", "arguments must be an object.")
                            else -> withTimeoutOrNull((expires - now).coerceAtMost(30_000)) {
                                executor.execute(command.optString("name"), command.getJSONObject("arguments"), expires)
                            } ?: ToolRegistry.error("expired", "Execution deadline elapsed. Verify device state; this command will not be repeated.")
                        }
                        withContext(NonCancellable + Dispatchers.IO) { journal.finish(key, result) }
                        ensureActive()
                        val response = DeviceToolsApi.request(target, "$id/result", JSONObject().put("result", result))
                        check(response.optBoolean("ok"))
                        withContext(Dispatchers.IO) { journal.posted(key) }
                    }
                } catch (e: CancellationException) { throw e }
                catch (_: ToolsAuthorizationRevoked) { stop(); mutableAvailability.value = "authorization_revoked"; return@launch }
                catch (_: ToolsUnsupported) { stop(); mutableAvailability.value = "unsupported_server"; return@launch }
                catch (_: Exception) { mutableAvailability.value = "offline" }
                delay(5_000)
            }
        }
    }
}
