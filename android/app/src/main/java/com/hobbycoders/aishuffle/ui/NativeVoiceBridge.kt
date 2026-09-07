package com.hobbycoders.aishuffle.ui

import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.lifecycle.Lifecycle
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.hobbycoders.aishuffle.account.PairingInput
import org.json.JSONObject

/** A foreground request can open native voice for this worker and one selected chat only. */
internal class NativeVoiceBridge(
    private val activity: ComponentActivity,
    private val web: WebView,
    private val origin: String,
    private val authorized: () -> Boolean,
    private val launch: (String) -> Unit,
) {
    private var installed = false
    private var opening = false
    private var page = 0
    private val handled = ArrayDeque<String>()

    fun install() {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return
        WebViewCompat.addWebMessageListener(web, "shuffleNativeVoice", setOf(PairingInput.origin(origin))) { _, message, source, mainFrame, reply ->
            if (!mainFrame || !PairingInput.sameOrigin(origin, source.toString()) || message.type != WebMessageCompat.TYPE_STRING) return@addWebMessageListener
            val request = runCatching { NativeVoiceRequest.parse(message.data.orEmpty()) }.getOrNull() ?: return@addWebMessageListener
            fun respond(ok: Boolean, error: String? = null) {
                val response = JSONObject().put("id", request.id).put("sessionId", request.sessionId).put("ok", ok)
                if (error != null) response.put("error", error)
                runCatching { reply.postMessage(response.toString()) }
            }
            if (!foreground()) { respond(false, "Return to this chat before starting voice."); return@addWebMessageListener }
            if (opening || request.id in handled) { respond(false, "Voice is already opening."); return@addWebMessageListener }
            opening = true
            handled.addLast(request.id)
            if (handled.size > 32) handled.removeFirst()
            val requestedPage = page
            // Reply first: the embedded caller must not wait while its activity is paused.
            respond(true)
            web.post {
                if (page != requestedPage || !foreground()) {
                    opening = false; respond(false, "Chat changed. Start voice again from the current chat.")
                } else {
                    try { launch(request.sessionId) }
                    catch (_: Exception) { opening = false; respond(false, "Voice could not open. Try again.") }
                }
            }
        }
        installed = true
    }

    private fun foreground() = !activity.isFinishing && !activity.isDestroyed && activity.hasWindowFocus() &&
        activity.lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED) && ChatAccessPolicy.document(origin, web.url, authorized())
    fun navigating() { page++; opening = false }
    fun resumed() { opening = false }
    fun close() { if (installed) WebViewCompat.removeWebMessageListener(web, "shuffleNativeVoice") }
}
