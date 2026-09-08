package com.hobbycoders.aishuffle.ui

import android.webkit.WebResourceResponse
import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.model.Server
import okhttp3.Call
import java.io.ByteArrayInputStream
import java.io.FilterInputStream
import java.io.IOException
import java.net.URI
import java.util.concurrent.ConcurrentHashMap

/** No cookie/session-token URL workaround: image/audio/video requests use the same paired auth as fetch. */
internal class WorkerAssetLoader(private val client: WorkerDownloadClient = WorkerDownloadClient()) {
    private val calls = ConcurrentHashMap.newKeySet<Call>()

    companion object {
        const val AUTH_MESSAGE = "This worker could not authorize a file. Update the worker, refresh chat, then pair again if needed."

        /** Old markdown stores /api links, resolved by the browser against the bundle origin. */
        fun workerUrl(base: String, url: String): String? = runCatching {
            when {
                ChatDownloadPolicy.url(base, url) -> url
                MobileUi.isAssetOrigin(url) && URI(url).rawPath.startsWith("/api/") -> {
                    val uri = URI(url)
                    val resolved = PairingInput.origin(base) + uri.rawPath + (uri.rawQuery?.let { "?$it" } ?: "")
                    resolved.takeIf { ChatDownloadPolicy.url(base, it) }
                }
                else -> null
            }
        }.getOrNull()

        private fun failure(code: Int, message: String) = WebResourceResponse(
            "text/plain", "utf-8", code, "Blocked", mapOf("Cache-Control" to "no-store"),
            ByteArrayInputStream(message.toByteArray(Charsets.UTF_8)))
    }

    fun load(server: Server, url: String, method: String, headers: Map<String, String>, token: String?,
             cookie: String?, userAgent: String, allowed: () -> Boolean, status: (String) -> Unit): WebResourceResponse {
        if (method != "GET" && method != "HEAD") return failure(405, "Only file reads are supported.")
        val target = workerUrl(server.baseUrl, url) ?: return failure(403, "External content blocked")
        if (token.isNullOrBlank() || !allowed()) return failure(401, "Refresh chat before opening files.")
        var activeCall: Call? = null
        try {
            val response = client.open(server, target, cookie, userAgent, allowed, { call ->
                activeCall?.let(calls::remove)
                activeCall = call
                calls.add(call)
            }, "Bearer $token", method, headers)
            try {
                // 304 cannot be represented by WebResourceResponse. With no-store, media has no
                // native cache to revalidate; signal a retry instead of inventing an empty 200.
                if (response.code == 304) {
                    response.close()
                    activeCall?.let(calls::remove)
                    return failure(502, "Refresh chat to reload this file.")
                }
                if (response.code in listOf(401, 403)) status(AUTH_MESSAGE)
                val body = requireNotNull(response.body)
                val mime = body.contentType()
                val responseHeaders = linkedMapOf(
                    "Cache-Control" to "no-store",
                    "Access-Control-Allow-Origin" to MobileUi.ORIGIN,
                    "Access-Control-Allow-Credentials" to "true",
                    "Access-Control-Expose-Headers" to "Content-Length, Content-Range, Accept-Ranges, Content-Disposition, ETag, Last-Modified",
                )
                // Do not install worker cookies or forward hop-by-hop/redirect headers into the bundle.
                for (name in listOf("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition", "ETag", "Last-Modified", "Content-Encoding")) {
                    response.header(name)?.let { responseHeaders[name] = it }
                }
                val stream = object : FilterInputStream(body.byteStream()) {
                    override fun read(): Int { checkOwner(); return super.read().also { checkOwner() } }
                    override fun read(bytes: ByteArray, offset: Int, length: Int): Int {
                        checkOwner(); return super.read(bytes, offset, length).also { checkOwner() }
                    }
                    private fun checkOwner() {
                        if (!allowed()) { close(); throw IOException("Chat authorization changed") }
                    }
                    override fun close() {
                        try { response.close() } finally { activeCall?.let(calls::remove) }
                    }
                }
                return WebResourceResponse(mime?.let { "${it.type}/${it.subtype}" } ?: "application/octet-stream",
                    mime?.charset()?.name(), response.code, response.message.ifBlank { "Worker response" }, responseHeaders, stream)
            } catch (error: Exception) {
                response.close()
                throw error
            }
        } catch (_: Exception) {
            activeCall?.let { it.cancel(); calls.remove(it) }
            if (allowed()) status("This file could not be loaded. Check the worker connection and Cloudflare Access settings, then refresh chat.")
            return failure(502, "The worker could not load this file.")
        }
    }

    fun cancel() { calls.forEach { it.cancel() }; calls.clear() }
}
