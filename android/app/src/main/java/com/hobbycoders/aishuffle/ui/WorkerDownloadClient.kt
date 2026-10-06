package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.model.Server
import okhttp3.Call
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import java.io.OutputStream
import java.util.concurrent.CancellationException
import java.util.concurrent.TimeUnit

/**
 * The worker answered a file request with a redirect off its own origin (for a Cloudflare
 * Access worker, the Access sign-in page). Credentials were not forwarded.
 */
internal class ExternalRedirectException :
    IllegalArgumentException("The file redirects outside this worker. Its credentials were not forwarded.")

/** Shared streaming transport for WebView assets and SAF downloads. Credentials never leave the paired HTTPS origin. */
internal class WorkerDownloadClient(
    client: OkHttpClient = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS).build()
) {
    // A media stream can outlive a short RPC timeout. Its owner cancels it on navigation/close.
    private val http = client.newBuilder().followRedirects(false).followSslRedirects(false)
        .callTimeout(0, TimeUnit.MILLISECONDS).build()

    /** Caller owns the returned response, including its streaming body. */
    fun open(server: Server, url: String, cookie: String?, userAgent: String,
             allowed: () -> Boolean, onCall: (Call) -> Unit, authorization: String? = null,
             method: String = "GET", headers: Map<String, String> = emptyMap()): Response {
        require(method == "GET" || method == "HEAD") { "Only file reads are supported." }
        var next = url
        repeat(6) { attempt ->
            require(ChatDownloadPolicy.url(server.baseUrl, next)) { "Only files from this worker can be saved." }
            if (!allowed()) throw CancellationException("Download cancelled")
            val request = Request.Builder().url(next).method(method, null)
                .header("User-Agent", userAgent).header("Accept-Encoding", "identity").apply {
                    // Never accept credentials, Host or forwarding headers supplied by a document.
                    headers.forEach { (name, value) ->
                        if (name.lowercase() in setOf("range", "if-range", "if-none-match", "if-modified-since", "accept")) header(name, value)
                    }
                    authorization?.let { header("Authorization", it) }
                    // An older APK may have left a different paired session in WebView's jar.
                    // Cookie auth precedes Bearer on workers: never let it choose this request's owner.
                    val cookies = if (authorization != null) cookie?.split(';')
                        ?.map(String::trim)?.filter { it.startsWith("CF_Authorization=") }?.joinToString("; ") else cookie
                    cookies?.takeIf { it.isNotBlank() }?.let { header("Cookie", it) }
                    if (server.hasCfAccess) {
                        header("CF-Access-Client-Id", server.cfAccessClientId!!)
                        header("CF-Access-Client-Secret", server.cfAccessClientSecret!!)
                    }
                }.build()
            val call = http.newCall(request)
            onCall(call)
            if (!allowed()) { call.cancel(); throw CancellationException("Download cancelled") }
            val response = call.execute()
            try {
                if (!allowed()) throw CancellationException("Download cancelled")
                if (response.code in 300..399 && response.code != 304) {
                    val redirected = response.header("Location")?.let { response.request.url.resolve(it) }?.toString()
                    if (redirected == null || !ChatDownloadPolicy.url(server.baseUrl, redirected))
                        throw ExternalRedirectException()
                    require(attempt < 5) { "The file redirected too many times. Try again." }
                    next = redirected
                } else {
                    return response
                }
            } catch (error: Exception) {
                response.close()
                throw error
            }
            response.close()
        }
        error("Unreachable redirect limit")
    }

    fun save(server: Server, url: String, cookie: String?, userAgent: String, output: OutputStream,
             allowed: () -> Boolean, onCall: (Call) -> Unit, authorization: String? = null) {
        open(server, url, cookie, userAgent, allowed, onCall, authorization).use { response ->
            require(response.code !in listOf(401, 403)) { "Your worker session expired or this worker needs an update. Refresh chat after updating the worker; pair again if needed." }
            require(response.isSuccessful) { "The worker could not send this file. Try again." }
            val body = requireNotNull(response.body) { "The worker sent an empty response." }
            ChatDownloadPolicy.copy(body.byteStream(), output, body.contentLength(), allowed)
        }
    }
}
