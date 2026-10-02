package com.hobbycoders.aishuffle.ui

import android.webkit.WebResourceResponse
import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.model.Server
import okhttp3.Call
import java.io.ByteArrayInputStream
import java.io.FilterInputStream
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap

/** No cookie/session-token URL workaround: image/audio/video requests use the same paired auth as fetch. */
internal class WorkerAssetLoader(private val client: WorkerDownloadClient = WorkerDownloadClient()) {
    private val calls = ConcurrentHashMap.newKeySet<Call>()

    companion object {
        const val AUTH_MESSAGE = "This worker could not authorize a file. Update the worker, refresh chat, then pair again if needed."

        /** A URL on the paired worker (the phone UI page's own origin), or null. */
        fun workerUrl(base: String, url: String): String? = url.takeIf { ChatDownloadPolicy.url(base, it) }

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
                val range = if (response.code == 206 && headers.keys.any { it.equals("Range", true) }) {
                    val match = Regex("bytes ([0-9]+)-([0-9]+)/([0-9]+)").matchEntire(response.header("Content-Range").orEmpty())
                        ?: throw IOException("Invalid partial file response")
                    match.groupValues.drop(1).map { it.toLong() }.also { (start, end, total) ->
                        require(start <= end && end < total) { "Invalid partial file bounds" }
                    }
                } else null
                val resourceSize = range?.get(2) ?: response.header("Content-Length")?.toLongOrNull() ?: body.contentLength()
                var initialSkipCredit = range?.get(0) ?: 0L
                var reading = false
                val responseHeaders = linkedMapOf(
                    "Cache-Control" to "no-store",
                    // Same-origin for the phone UI page; kept explicit for crossorigin media elements.
                    "Access-Control-Allow-Origin" to PairingInput.origin(server.baseUrl),
                    "Access-Control-Allow-Credentials" to "true",
                    "Access-Control-Expose-Headers" to "Content-Length, Content-Range, Accept-Ranges, Content-Disposition, ETag, Last-Modified",
                )
                // Do not install worker cookies or forward hop-by-hop/redirect headers into the page.
                for (name in listOf("Content-Type", "Content-Range", "Accept-Ranges", "Content-Disposition", "ETag", "Last-Modified", "Content-Encoding")) {
                    response.header(name)?.let { responseHeaders[name] = it }
                }
                // Chromium's InputStreamReader::Seek computes bounds from available(),
                // then skips the request's original offset even for a supplied 206. Expose
                // the full resource size and credit that initial skip: the worker has
                // already selected the window. Otherwise bytes 2-4 = "abc" becomes "c".
                // Chromium also emits Content-Length; forwarding it creates duplicates.
                val stream = object : FilterInputStream(body.byteStream()) {
                    override fun available(): Int {
                        checkOwner()
                        return if (!reading && resourceSize >= 0) resourceSize.coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
                        else super.available()
                    }
                    override fun skip(count: Long): Long {
                        checkOwner()
                        val credit = count.coerceAtLeast(0).coerceAtMost(initialSkipCredit)
                        initialSkipCredit -= credit
                        val skipped = credit + if (count > credit) super.skip(count - credit) else 0L
                        checkOwner()
                        return skipped
                    }
                    private fun beginRead() { checkOwner(); reading = true; initialSkipCredit = 0L }
                    override fun read(): Int { beginRead(); return super.read().also { checkOwner() } }
                    override fun read(bytes: ByteArray, offset: Int, length: Int): Int {
                        beginRead(); return super.read(bytes, offset, length).also { checkOwner() }
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
