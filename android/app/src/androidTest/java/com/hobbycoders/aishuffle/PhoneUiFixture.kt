package com.hobbycoders.aishuffle

import android.content.Context
import android.webkit.WebResourceResponse
import com.hobbycoders.aishuffle.ui.ChatActivity
import com.hobbycoders.aishuffle.ui.MobileUi
import java.io.ByteArrayInputStream
import java.io.IOException

/**
 * Plays the fixture worker's `/mobile/` for chat smoke tests. A real worker
 * serves the phone UI (app/mobile_ui.py); the fixture worker has no network, so
 * this answers those requests from the APK's offline demo, which is the same
 * `mobile/` build. API requests are untouched.
 */
object PhoneUiFixture {
    fun install(context: Context) {
        ChatActivity.uiFixture = { request -> serve(context, request.url.path.orEmpty()) }
    }

    fun clear() { ChatActivity.uiFixture = null }

    private fun serve(context: Context, path: String): WebResourceResponse {
        val rest = path.removePrefix(MobileUi.PATH.removeSuffix("/")).trimStart('/')
        // Same rule as the worker: a name with an extension is a file, anything else is a UI route.
        val name = if ('.' in rest.substringAfterLast('/')) rest else "index.html"
        return try {
            WebResourceResponse(mime(name), "utf-8", context.assets.open("offline-demo/$name"))
        } catch (_: IOException) {
            WebResourceResponse("text/plain", "utf-8", 404, "Not Found", emptyMap(), ByteArrayInputStream(ByteArray(0)))
        }
    }

    private fun mime(name: String) = when (name.substringAfterLast('.').lowercase()) {
        "html" -> "text/html"
        "js", "mjs" -> "text/javascript"
        "css" -> "text/css"
        "json", "webmanifest" -> "application/json"
        "svg" -> "image/svg+xml"
        "png" -> "image/png"
        "woff2" -> "font/woff2"
        else -> "application/octet-stream"
    }
}
