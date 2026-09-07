package com.hobbycoders.aishuffle

import com.hobbycoders.aishuffle.presence.WorkerPresenceProbe
import com.hobbycoders.aishuffle.presence.WorkerPresenceMonitor
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody

/** Test transport only: automatic worker checks must never visit a real saved/default host. */
internal class PresenceFixture(validatedNetwork: Boolean? = null, handler: (Request) -> Response = { response(it, 503, "{}") }) : AutoCloseable {
    private val field = WorkerPresenceProbe::class.java.getDeclaredField("client").apply { isAccessible = true }
    private val previous = field.get(null)
    private val networkField = WorkerPresenceMonitor::class.java.getDeclaredField("networkReader").apply { isAccessible = true }
    private val previousNetwork = networkField.get(null)
    init {
        field.set(null, OkHttpClient.Builder().addInterceptor { handler(it.request()) }.build())
        if (validatedNetwork != null) network("synthetic-network", validatedNetwork)
    }
    fun network(id: String, validated: Boolean) {
        val reader: (android.content.Context) -> WorkerPresenceMonitor.NetworkState = { WorkerPresenceMonitor.NetworkState(id, validated) }
        networkField.set(null, reader)
    }
    override fun close() { field.set(null, previous); networkField.set(null, previousNetwork) }
    companion object {
        fun response(request: Request, code: Int, body: String): Response = Response.Builder().request(request)
            .protocol(Protocol.HTTP_1_1).code(code).message("Synthetic presence fixture")
            .body(body.toResponseBody("application/json".toMediaType())).build()
    }
}
