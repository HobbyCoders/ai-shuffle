package com.hobbycoders.aishuffle.tools

import android.app.Activity
import android.app.Application
import android.content.Context
import android.content.Intent
import android.os.Bundle
import com.hobbycoders.aishuffle.model.Server
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
    // A launch or locally cancelled approval may defer pause shutdown only through result delivery.
    private var handoffInFlight = false
    private val mutableAvailability = MutableStateFlow("foreground_required")
    val availability: StateFlow<String> = mutableAvailability

    /** Call onResume (or after account/server selection); detach onPause. Main thread only. */
    fun attach(activity: Activity, server: Server) {
        check(android.os.Looper.myLooper() == android.os.Looper.getMainLooper())
        if (!authorizedTarget(activity, server)) { stop(); mutableAvailability.value = "authorization_revoked"; return }
        if (this.server == server && polling?.isActive == true) {
            this.activity = WeakReference(activity)
            ToolApprovals.attach(activity)
            return
        }
        val store = com.hobbycoders.aishuffle.data.ServerStore.get(activity)
        if (store.selectedToolsServerId != null && store.selectedToolsServerId != server.id) ToolsPreferences(activity).setCrossAppEnabled(false)
        deactivate()
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
        // Clear the execution host BEFORE resolving consent: the continuation may resume inline.
        this.activity.clear()
        if (ToolApprovals.pending.value != null) handoffInFlight = true
        ToolApprovals.detach(activity)
        if (handoffInFlight) {
            mutableAvailability.value = "handoff_result_pending"
            return
        }
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
    // Consent temporarily owns window focus without pausing/detaching its host activity.
    internal fun hasAttachedActivity(): Boolean = activity.get()?.let { !it.isFinishing && !it.isDestroyed } == true
    internal fun hasForegroundActivity(): Boolean = hasAttachedActivity() && activity.get()?.hasWindowFocus() == true
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
        handoffInFlight = true
        try { host.startActivity(intent) }
        catch (failure: Exception) { handoffInFlight = false; throw failure }
        return JSONObject().put("ok", true).put("status", "launched_confirmation")
            .put("message", "Opened Android UI. Completion/save/send is controlled by the user and has not been verified.")
    }
    /** Runs after the bounded result POST (or failure), never permits another foreground action off screen. */
    internal fun finishHandoff(epoch: Long) {
        if (generation != epoch || !handoffInFlight) return
        handoffInFlight = false
        if (activity.get() == null && !hasCrossAppHost()) {
            deactivate()
            server = null
            mutableAvailability.value = "foreground_required"
        }
    }
    private fun deactivate() {
        handoffInFlight = false
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
        if (!com.hobbycoders.aishuffle.net.DeviceKeyRenewal.sameAuthorization(target, saved)) return@runCatching false
        if (target.accountId == null) return@runCatching true
        val session = com.hobbycoders.aishuffle.account.AccountStore(app).session()
        session != null && session.accountId == target.accountId && session.hub == target.hubUrl
    }.getOrDefault(false)

    // Shared by the production poller and lifecycle-ordering regression tests.
    internal fun canExecute(epoch: Long): Boolean =
        generation == epoch && !handoffInFlight && (activity.get() != null || hasCrossAppHost())

    /** The production poller wiring and handoff boundary, also used by JVM lifecycle tests. */
    internal suspend fun pollCommands(
        journal: CommandJournal, scopeId: String, epoch: Long, enabled: Boolean,
        executor: DeviceToolExecutor, request: suspend (String, JSONObject?) -> JSONObject,
    ) {
        try {
            DeviceCommandPoller(journal, scopeId, request, { canExecute(epoch) },
                executor::execute, { mutableAvailability.value = "queue_full" }).pollOnce(enabled)
        } finally {
            finishHandoff(epoch)
        }
    }

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
                    pollCommands(journal, scopeId, currentGeneration, enabled, executor) { path, body ->
                        DeviceToolsApi.request(target, path, body)
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
