package com.hobbycoders.aishuffle.tools

import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

internal class ToolsAuthorizationRevoked : IOException("Device authorization expired or was revoked.")
internal class ToolsUnsupported : IOException("This server does not support Android tools.")

internal object DeviceToolsApi {
    private val client = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(10, TimeUnit.SECONDS).readTimeout(15, TimeUnit.SECONDS).callTimeout(20, TimeUnit.SECONDS).build()
    suspend fun request(server: Server, path: String, body: JSONObject? = null): JSONObject {
        val origin = server.baseUrl.toHttpUrl()
        require(origin.scheme == "https")
        require(origin.username.isEmpty() && origin.password.isEmpty() && origin.query == null && origin.fragment == null)
        val url = origin.newBuilder().addPathSegments("api/v1/mobile/tools").apply {
            path.split('/').forEach { addPathSegment(it) }
        }.build()
        require(url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port)
        val builder = Request.Builder().url(url).header("Authorization", "Bearer ${server.apiKey}")
        if (server.hasCfAccess) {
            builder.header("CF-Access-Client-Id", server.cfAccessClientId!!)
            builder.header("CF-Access-Client-Secret", server.cfAccessClientSecret!!)
        }
        if (body != null) builder.post(body.toString().toRequestBody("application/json; charset=utf-8".toMediaType()))
        return suspendCancellableCoroutine { continuation ->
            val call = client.newCall(builder.build())
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) { if (!continuation.isCancelled) continuation.resumeWithException(IOException("Device tools connection unavailable.")) }
                override fun onResponse(call: Call, response: Response) {
                    response.use {
                        try {
                            if (response.code in setOf(401, 403)) throw ToolsAuthorizationRevoked()
                            if (response.code in setOf(404, 405, 501)) throw ToolsUnsupported()
                            check(response.isSuccessful) { "Device tools HTTP ${response.code}" }
                            val source = response.body?.source() ?: error("Empty response")
                            check(!source.request(262_145)) { "Response too large" }
                            val bytes = source.buffer.readByteArray()
                            if (!continuation.isCancelled) continuation.resume(JSONObject(String(bytes, Charsets.UTF_8)))
                        } catch (e: ToolsUnsupported) { if (!continuation.isCancelled) continuation.resumeWithException(e)
                        } catch (e: ToolsAuthorizationRevoked) { if (!continuation.isCancelled) continuation.resumeWithException(e)
                        } catch (_: Exception) { if (!continuation.isCancelled) continuation.resumeWithException(IOException("Invalid device tools response (HTTP ${response.code}).")) }
                    }
                }
            })
        }
    }
}
