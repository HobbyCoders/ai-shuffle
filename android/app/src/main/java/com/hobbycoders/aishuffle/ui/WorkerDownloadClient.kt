package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.model.Server
import okhttp3.Call
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.OutputStream
import java.util.concurrent.CancellationException
import java.util.concurrent.TimeUnit

/** The paired-session Bearer, cookies and optional Access headers are only ever sent to this worker's exact HTTPS origin. */
internal class WorkerDownloadClient(
    client: OkHttpClient = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS).callTimeout(5, TimeUnit.MINUTES).build()
) {
    private val http = client.newBuilder().followRedirects(false).followSslRedirects(false).build()

    fun save(server: Server, url: String, cookie: String?, userAgent: String, output: OutputStream,
             allowed: () -> Boolean, onCall: (Call) -> Unit, authorization: String? = null) {
        var next = url
        repeat(6) { attempt ->
            require(ChatDownloadPolicy.url(server.baseUrl, next)) { "Only files from this worker can be saved." }
            if (!allowed()) throw CancellationException("Download cancelled")
            val request = Request.Builder().url(next).header("User-Agent", userAgent).apply {
                authorization?.let { header("Authorization", it) }
                cookie?.let { header("Cookie", it) }
                if (server.hasCfAccess) {
                    header("CF-Access-Client-Id", server.cfAccessClientId!!)
                    header("CF-Access-Client-Secret", server.cfAccessClientSecret!!)
                }
            }.build()
            val call = http.newCall(request)
            onCall(call)
            if (!allowed()) { call.cancel(); throw CancellationException("Download cancelled") }
            call.execute().use { response ->
                if (response.code in listOf(301, 302, 303, 307, 308)) {
                    val redirected = response.header("Location")?.let { response.request.url.resolve(it) }?.toString()
                    require(redirected != null && ChatDownloadPolicy.url(server.baseUrl, redirected)) {
                        "The file redirects outside this worker. Its credentials were not forwarded."
                    }
                    require(attempt < 5) { "The file redirected too many times. Try again." }
                    next = redirected
                } else {
                    require(response.code !in listOf(401, 403)) { "Your worker session expired. Refresh chat, then download again." }
                    require(response.isSuccessful) { "The worker could not send this file. Try again." }
                    val body = requireNotNull(response.body) { "The worker sent an empty response." }
                    ChatDownloadPolicy.copy(body.byteStream(), output, body.contentLength(), allowed)
                    return
                }
            }
        }
    }
}
