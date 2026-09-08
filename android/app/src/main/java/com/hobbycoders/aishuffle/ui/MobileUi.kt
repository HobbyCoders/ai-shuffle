package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.account.PairingInput
import org.json.JSONObject

/**
 * The app's own copy of the mobile UI.
 *
 * `mobile/` at the repository root builds the UI; `android/scripts/sync-mobile-ui.sh`
 * copies it to `assets/mobile-ui/`, and ChatActivity serves it through
 * WebViewAssetLoader at [INDEX_URL]. A worker is never asked for the UI — it
 * only answers the page's HTTP/WebSocket calls, authenticated with the paired
 * session as a Bearer token that [bootstrapScript] hands the page before any
 * of its own script runs (contract: frontend/src/lib/api/runtime.ts).
 */
object MobileUi {
    /** WebViewAssetLoader's default domain. Only this origin may load in the chat WebView. */
    const val ORIGIN = "https://appassets.androidplatform.net"
    const val ASSET_DIR = "mobile-ui"
    /** Path prefix WebViewAssetLoader maps to assets/mobile-ui/ (the app's `paths.base`). */
    const val PATH = "/assets/$ASSET_DIR/"
    /** The document the WebView loads. */
    const val INDEX_URL = ORIGIN + PATH
    const val INDEX_ASSET = "$ASSET_DIR/index.html"

    fun isAssetOrigin(url: String?): Boolean = url != null && PairingInput.sameOrigin(ORIGIN, url)

    /** The bundle root (with or without its trailing slash) is served as index.html. */
    fun isIndexPath(path: String?): Boolean = path == PATH || path == PATH.removeSuffix("/")

    /** Document-start script installing `window.__SHUFFLE_MOBILE__`. */
    fun bootstrapScript(apiOrigin: String, token: String, version: String?, deviceName: String?, sendCookies: Boolean): String {
        val config = JSONObject().put("apiOrigin", PairingInput.origin(apiOrigin)).put("token", token).put("sendCookies", sendCookies)
        if (!version.isNullOrBlank()) config.put("version", version.trim())
        if (!deviceName.isNullOrBlank()) config.put("deviceName", deviceName.trim())
        return "window.__SHUFFLE_MOBILE__ = ${config};"
    }

    /** Hands an already-loaded page a fresh token without a reload. */
    fun tokenUpdateScript(token: String): String {
        val quoted = JSONObject.quote(token)
        return "if (typeof window.shuffleMobileSetToken === 'function') { window.shuffleMobileSetToken($quoted); } " +
            "else if (window.__SHUFFLE_MOBILE__) { window.__SHUFFLE_MOBILE__.token = $quoted; }"
    }
}
