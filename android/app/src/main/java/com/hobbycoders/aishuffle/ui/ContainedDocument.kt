package com.hobbycoders.aishuffle.ui

import android.webkit.CookieManager
import android.webkit.WebResourceResponse
import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.model.Server
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.ByteArrayInputStream
import java.util.concurrent.TimeUnit

/** Adds a native-enforced CSP before a document can run script or open a socket.
 * WebView interception does not see WebSockets, so URL interception alone is insufficient.
 */
internal object ContainedDocument {
    private val http = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(15, TimeUnit.SECONDS).callTimeout(25, TimeUnit.SECONDS).build()
    fun load(server: Server, url: String, userAgent: String): WebResourceResponse {
        if (!PairingInput.sameOrigin(server.baseUrl, url)) return blocked()
        return try {
            val request = Request.Builder().url(url).header("User-Agent", userAgent).apply {
                CookieManager.getInstance().getCookie(server.baseUrl)?.let { header("Cookie", it) }
                if (server.hasCfAccess) {
                    header("CF-Access-Client-Id", server.cfAccessClientId!!)
                    header("CF-Access-Client-Secret", server.cfAccessClientSecret!!)
                }
            }.build()
            http.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return blocked("Chat could not open. Return to Shuffle and refresh your session, or pair the server again.")
                val body = response.body ?: return blocked()
                val type = body.contentType()
                val mime = type?.let { "${it.type}/${it.subtype}" } ?: "text/html"
                val headers = response.headers.toMap().toMutableMap()
                headers.keys.filter { it.equals("Set-Cookie", true) || it.equals("Content-Security-Policy", true) }.toList().forEach { headers.remove(it) }
                val socket = "wss://" + server.baseUrl.removePrefix("https://")
                headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self' $socket; frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Cache-Control"] = "no-store"
                WebResourceResponse(mime, type?.charset()?.name() ?: "UTF-8", response.code, "OK", headers, ByteArrayInputStream(body.bytes()))
            }
        } catch (_: Exception) { blocked("Cannot reach this server. Check your connection and tap Refresh chat.") }
    }
    private fun blocked(message: String = "This content is blocked.") = WebResourceResponse("text/plain", "UTF-8", 403, "Blocked", mapOf("Cache-Control" to "no-store"), ByteArrayInputStream(message.toByteArray()))
}
