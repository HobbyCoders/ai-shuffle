package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.account.PairingInput
import org.json.JSONArray
import org.json.JSONObject
import java.net.URI

/**
 * The phone UI, served by the paired worker at `/mobile/`.
 *
 * `mobile/` at the repository root builds it; every worker ships it beside the
 * desktop UI (app/mobile_ui.py), so a UI change reaches this app with a worker
 * update and never needs a new APK. The app owns only the native side: pairing,
 * phone tools, permissions and the bridge described by [BRIDGE_VERSION],
 * [CAPABILITIES] and [SHELL_BRIDGE]. The paired session reaches the page as a Bearer token through
 * [bootstrapScript], installed before any page script runs (contract:
 * frontend/src/lib/api/runtime.ts). See docs/mobile-desktop-ui-split.md.
 */
object MobileUi {
    /** Path the worker serves the phone UI under (the build's `paths.base` plus `/`). */
    const val PATH = "/mobile/"

    /**
     * Version of the native bridge this app offers the page. Bump it when a
     * capability changes shape; add a capability name when one is added, so a
     * newer worker UI can tell an older app apart and degrade.
     */
    const val BRIDGE_VERSION = 1

    /**
     * The page reports its call's state (`voice-call-state`) and answers the
     * [voiceCallEventScript] commands, so a call keeps running behind the lock
     * screen with an ongoing notification (VoiceCallService).
     */
    const val VOICE_CALL_CONTROLS = "voice-call-controls"

    /**
     * The shell opens the app's own Settings (account, sign out, phone tools)
     * on `{"type":"app-settings"}`. Without it the page sends `workers`, whose
     * screen has the Settings button.
     */
    const val APP_SETTINGS = "app-settings"
    val CAPABILITIES = listOf("file-save", "file-chooser", "microphone", "device-tools", "voice-routing", VOICE_CALL_CONTROLS,
        WorkerSwitch.CAPABILITY, APP_SETTINGS)

    /**
     * Web-message bridge the page uses to tell the shell about its own chrome
     * (`window.shuffleShell.postMessage(json)`), scoped like `shuffleFileSave`
     * to the top-level phone UI page. Messages ([shellMessage]):
     *  - `{"type":"worker-bar","shown":true|false}`: the page draws its own
     *    "Connected to" bar, so the shell hides its worker-name bar. The shell
     *    shows its bar until told, so an older worker UI keeps it.
     *  - `{"type":"workers"}`: open the native Workers screen (pair, rename,
     *    remove), which the hidden bar's menu used to reach.
     *  - `{"type":"app-settings"}`: open the app's own Settings screen (account,
     *    sign out, trusted devices, phone tools, delete account), which the
     *    hidden bar left out of reach while connected ([APP_SETTINGS]).
     *  - `{"type":"voice-call","active":true|false}`: a call started or ended
     *    (headset routing and the call's foreground service follow it).
     *  - `{"type":"voice-call-state","status":"connecting"|"live"|"reconnecting",
     *    "muted":true|false,"since":<epoch ms>,"title":"…"}`: the live call's
     *    state for its notification ([VOICE_CALL_CONTROLS]). Ignored when no call
     *    is active, so a late update never restarts an ended call.
     */
    const val SHELL_BRIDGE = "shuffleShell"
    private const val MAX_SHELL_MESSAGE = 256

    sealed class ShellMessage {
        data class WorkerBar(val shown: Boolean) : ShellMessage()
        data class VoiceCall(val active: Boolean) : ShellMessage()
        data class VoiceCallState(val status: String, val muted: Boolean, val since: Long?, val title: String?) : ShellMessage()
        object OpenWorkers : ShellMessage()
        object OpenAppSettings : ShellMessage()
    }

    /** Parses one `shuffleShell` message; null for anything unknown or malformed. */
    fun shellMessage(raw: String?): ShellMessage? {
        if (raw == null || raw.length > MAX_SHELL_MESSAGE) return null
        val json = runCatching { JSONObject(raw) }.getOrNull() ?: return null
        return when (json.optString("type")) {
            "worker-bar" -> (json.opt("shown") as? Boolean)?.let { ShellMessage.WorkerBar(it) }
            "workers" -> ShellMessage.OpenWorkers
            APP_SETTINGS -> ShellMessage.OpenAppSettings
            "voice-call" -> (json.opt("active") as? Boolean)?.let { ShellMessage.VoiceCall(it) }
            "voice-call-state" -> voiceCallState(json)
            else -> null
        }
    }

    private val CALL_STATUSES = setOf("connecting", "live", "reconnecting")
    const val MAX_CALL_TITLE = 60

    private fun voiceCallState(json: JSONObject): ShellMessage.VoiceCallState? {
        val status = json.opt("status") as? String ?: return null
        val muted = json.opt("muted") as? Boolean ?: return null
        if (status !in CALL_STATUSES) return null
        val since = (json.opt("since") as? Number)?.toLong()?.takeIf { it > 0 }
        val title = (json.opt("title") as? String)?.filterNot { it.isISOControl() }?.trim()?.take(MAX_CALL_TITLE)?.ifBlank { null }
        return ShellMessage.VoiceCallState(status, muted, since, title)
    }

    /** What the call notification asks the page to do (`shufflevoicecall` event `detail.command`). */
    enum class VoiceCallCommand(val wire: String) { MUTE("mute"), UNMUTE("unmute"), HANGUP("hangup"), NOTIFICATIONS_OFF("notifications-off") }

    /** Dispatches one call command into the page; the page's own call code acts on it. */
    fun voiceCallEventScript(command: VoiceCallCommand): String =
        "window.dispatchEvent(new CustomEvent('shufflevoicecall', { detail: { command: ${JSONObject.quote(command.wire)} } }))"

    /** The document the chat WebView loads from a worker. */
    fun indexUrl(base: String): String = PairingInput.origin(base) + PATH

    /**
     * Whether `url` is the phone UI on this worker: same origin, path `/mobile`
     * or below. The only documents the chat WebView may show, and the only ones
     * that get the session or a native capability.
     */
    fun isUiUrl(base: String, url: String?): Boolean = url != null && runCatching {
        val path = URI(url).rawPath.orEmpty()
        PairingInput.sameOrigin(base, url) && (path == PATH.removeSuffix("/") || path.startsWith(PATH)) &&
            !path.split('/').any { it == ".." || it.equals("%2e%2e", true) }
    }.getOrDefault(false)

    /**
     * Document-start script installing `window.__SHUFFLE_MOBILE__`. WebView
     * scopes it to the worker's origin; the guard keeps it to the top-level
     * phone UI page, never a frame or another worker page.
     */
    fun bootstrapScript(apiOrigin: String, token: String, version: String?, deviceName: String?, sendCookies: Boolean,
                        capabilities: List<String> = CAPABILITIES): String {
        val config = JSONObject().put("apiOrigin", PairingInput.origin(apiOrigin)).put("token", token).put("sendCookies", sendCookies)
        if (!version.isNullOrBlank()) config.put("version", version.trim())
        if (!deviceName.isNullOrBlank()) config.put("deviceName", deviceName.trim())
        config.put("native", JSONObject().put("platform", "android").put("bridge", BRIDGE_VERSION).put("capabilities", JSONArray(capabilities)))
        return "if (window.top === window && location.pathname.indexOf(${JSONObject.quote(PATH)}) === 0) { window.__SHUFFLE_MOBILE__ = ${config}; }"
    }

    /** Hands an already-loaded page a fresh token without a reload. */
    fun tokenUpdateScript(token: String): String {
        val quoted = JSONObject.quote(token)
        return "if (typeof window.shuffleMobileSetToken === 'function') { window.shuffleMobileSetToken($quoted); } " +
            "else if (window.__SHUFFLE_MOBILE__) { window.__SHUFFLE_MOBILE__.token = $quoted; }"
    }

    /** What to show when the worker answers the UI page with an HTTP error. */
    fun loadFailure(status: Int): String = if (status == 404)
        "This worker is too old to serve the phone UI. Update the worker, then refresh chat."
    else "The worker could not open chat (HTTP $status). Refresh chat to try again."
}
