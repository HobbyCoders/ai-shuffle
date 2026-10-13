package com.hobbycoders.aishuffle.presence

import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.Call
import okhttp3.Callback
import okhttp3.CookieJar
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException
import java.io.InterruptedIOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

/** Public, origin-bound probes. Neither an account token nor a worker/device key is sent. */
internal object WorkerPresenceProbe {
    private const val MAX_BODY = 8_192L
    private val client = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .cookieJar(CookieJar.NO_COOKIES).cache(null).retryOnConnectionFailure(false)
        .connectTimeout(6, TimeUnit.SECONDS).readTimeout(6, TimeUnit.SECONDS).callTimeout(6, TimeUnit.SECONDS).build()

    internal fun request(server: Server): Request {
        val origin = PairingInput.origin(server.baseUrl)
        require(origin == server.baseUrl) { "Worker origin changed." }
        if (server.accountId != null) require(!server.hubServerId.isNullOrBlank()) { "Worker identity is unavailable." }
        return Request.Builder().url(origin + if (server.accountId == null) "/api/v1/health" else "/api/v1/mobile/hub/endpoint")
            .header("Accept", "application/json").header("Cache-Control", "no-cache, no-store").apply {
                if (server.hasCfAccess) {
                    header("CF-Access-Client-Id", requireNotNull(server.cfAccessClientId))
                    header("CF-Access-Client-Secret", requireNotNull(server.cfAccessClientSecret))
                }
            }.build()
    }

    internal fun parse(server: Server, status: Int, body: String): ProbeResult {
        if (status in setOf(502, 504, 521, 522, 523, 524, 530)) return ProbeResult.UNREACHABLE
        if (status != 200 || body.toByteArray(Charsets.UTF_8).size > MAX_BODY) return ProbeResult.UNAVAILABLE
        val json = runCatching { JSONObject(body) }.getOrNull() ?: return ProbeResult.UNAVAILABLE
        val ready = if (server.accountId != null) {
            !server.hubServerId.isNullOrBlank() && json.opt("ready") == true && json.opt("protected_ingress") == true &&
                json.opt("server_id") == server.hubServerId
        } else {
            json.opt("status") == "healthy" && (json.opt("service") as? String)?.isNotBlank() == true &&
                (json.opt("version") as? String)?.isNotBlank() == true && json.opt("authenticated") is Boolean
        }
        return if (ready) ProbeResult.ONLINE else ProbeResult.UNAVAILABLE
    }

    internal fun failure(error: IOException, timedOut: Boolean = false): ProbeResult =
        if (error is ConnectException || error is SocketTimeoutException || timedOut && error is InterruptedIOException)
            ProbeResult.UNREACHABLE else ProbeResult.UNAVAILABLE

    suspend fun check(server: Server): ProbeResult {
        val request = runCatching { request(server) }.getOrNull() ?: return ProbeResult.UNAVAILABLE
        return suspendCancellableCoroutine { continuation ->
            val call = client.newCall(request)
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, error: IOException) {
                    if (continuation.isActive) continuation.resume(failure(error, call.isCanceled()))
                }
                override fun onResponse(call: Call, response: Response) {
                    val result = response.use {
                        // Reject cache hits and unexpected final URLs even if the transport is replaced.
                        if (it.cacheResponse != null || it.request.url != request.url) return@use ProbeResult.UNAVAILABLE
                        if (it.code != 200) return@use parse(server, it.code, "")
                        val body = it.body ?: return@use ProbeResult.UNAVAILABLE
                        if (body.contentLength() > MAX_BODY) return@use ProbeResult.UNAVAILABLE
                        runCatching {
                            val source = body.source()
                            source.request(MAX_BODY + 1)
                            if (source.buffer.size > MAX_BODY) ProbeResult.UNAVAILABLE else parse(server, it.code, source.readUtf8())
                        }.getOrElse { error -> if (error is IOException) failure(error, call.isCanceled()) else ProbeResult.UNAVAILABLE }
                    }
                    if (continuation.isActive) continuation.resume(result)
                }
            })
        }
    }
}
