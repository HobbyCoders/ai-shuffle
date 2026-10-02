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
 * phone tools, permissions and the bridge described by [BRIDGE_VERSION] and
 * [CAPABILITIES]. The paired session reaches the page as a Bearer token through
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
    val CAPABILITIES = listOf("file-save", "file-chooser", "microphone", "device-tools")

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
