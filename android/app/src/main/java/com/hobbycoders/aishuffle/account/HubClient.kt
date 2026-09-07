package com.hobbycoders.aishuffle.account

import android.os.Build
import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException
import java.net.UnknownHostException
import javax.net.ssl.SSLException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

class HubFailure(val status: Int, message: String) : IOException(message)

object HubClient {
    internal fun connectionFailure(error: IOException): HubFailure = HubFailure(0, when (error) {
        is UnknownHostException -> "This address is not available on your network yet. Check your connection and retry shortly."
        is SSLException -> "A secure connection could not be verified. Check the worker’s HTTPS setup before retrying."
        else -> "Could not reach the service. Check your connection and retry."
    })
    private val client = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(15, TimeUnit.SECONDS).callTimeout(25, TimeUnit.SECONDS).build()
    fun device(id: String): JSONObject = JSONObject().put("device_id", id).put("device_name", "${Build.MANUFACTURER} ${Build.MODEL}").put("platform", "android")
    fun segment(id: String): String {
        require(id.matches(Regex("[A-Za-z0-9_-]+"))) { "The service returned an invalid identifier." }
        return id
    }
    suspend fun call(hub: String, path: String, method: String = "GET", body: JSONObject? = null, token: String? = null): JSONObject {
        val origin = PairingInput.origin(hub)
        require(path.startsWith("/v1/") || path.startsWith("/api/v1/mobile/hub/"))
        val request = Request.Builder().url(origin + path).apply {
            if (token != null) header("Authorization", "Bearer $token")
            method(method, if (method == "GET") null else (body ?: JSONObject()).toString().toRequestBody("application/json".toMediaType()))
        }.build()
        return suspendCancellableCoroutine { continuation ->
            val call = client.newCall(request)
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    if (continuation.isActive) continuation.resumeWithException(connectionFailure(e))
                }
                override fun onResponse(call: Call, response: Response) {
                    response.use {
                        try {
                            if (!it.isSuccessful) throw HubFailure(it.code, when (it.code) {
                                401 -> "Your authorization is no longer valid. Sign in again or use a new pairing code."
                                403 -> "This device is not allowed to do that. Ask a trusted device to approve it."
                                404, 405, 501 -> "This host does not support this account or pairing action. Check the selected hub/server mode and update the host."
                                409, 410 -> "This pairing or ticket was used, revoked or expired. Create a new code and try again."
                                429 -> "Too many attempts. Wait a moment before trying again."
                                else -> "The host could not complete the request (${it.code}). Check the address and try again."
                            })
                            val json = JSONObject(it.body?.string() ?: "{}")
                            if (continuation.isActive) continuation.resume(json)
                        } catch (e: Exception) {
                            if (continuation.isActive) continuation.resumeWithException(if (e is HubFailure) e else HubFailure(0, "The host returned an unsupported response. Update the host and retry."))
                        }
                    }
                }
            })
        }
    }
}
