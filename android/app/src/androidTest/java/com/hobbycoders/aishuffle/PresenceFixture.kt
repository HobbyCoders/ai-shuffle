package com.hobbycoders.aishuffle

import com.hobbycoders.aishuffle.presence.WorkerPresenceProbe
import com.hobbycoders.aishuffle.presence.WorkerPresenceMonitor
import com.hobbycoders.aishuffle.model.Server
import java.time.Instant
import okhttp3.OkHttpClient
import okio.Buffer
import org.json.JSONObject
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
        // Model the real renewal endpoint at the HTTP boundary. The app must
        // validate and durably persist this result itself before opening chat;
        // do not seed its renewal cache or substitute its credential store.
        fun renewDeviceKey(request: Request, server: Server): Response {
            check(request.url.toString() == "${server.baseUrl}/api/v1/mobile/key/renew" && request.method == "POST")
            check(request.header("Authorization") == "Bearer ${server.apiKey}" && request.header("Cookie") == null)
            val body = Buffer().also { requireNotNull(request.body).writeTo(it) }.readUtf8()
            check(JSONObject(body).length() == 0)
            val receipt = JSONObject().put("device_id", server.deviceId).put("api_key", server.apiKey)
                .put("rotated", false).put("expires_at", Instant.now().plusSeconds(3600).toString())
            return response(request, 200, receipt.toString()).newBuilder().header("Content-Type", "application/json").build()
        }

        fun response(request: Request, code: Int, body: String): Response = Response.Builder().request(request)
            .protocol(Protocol.HTTP_1_1).code(code).message("Synthetic presence fixture")
            .body(body.toResponseBody("application/json".toMediaType())).build()
    }
}
