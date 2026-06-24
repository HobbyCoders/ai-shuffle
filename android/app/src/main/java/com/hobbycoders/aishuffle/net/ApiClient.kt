package com.hobbycoders.aishuffle.net

import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Result of an [ApiClient] call. Never throws to the caller. */
sealed class ApiResult<out T> {
    data class Success<T>(val data: T) : ApiResult<T>()
    data class Failure(val message: String) : ApiResult<Nothing>()
}

/** Data returned by the device-registration endpoint. */
data class RegistrationResult(
    val apiKey: String,
    val deviceId: String,
    val expiresAt: String?,
)

/**
 * Thin OkHttp wrapper around the AI Shuffle mobile API. Every call is suspending
 * and runs on [Dispatchers.IO]; callers must never touch the main thread for
 * networking.
 */
object ApiClient {

    private const val JSON_MEDIA = "application/json; charset=utf-8"

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .build()
    }

    /**
     * POST {base}/api/v1/mobile/register — no auth header.
     * The returned api_key is shown only once and must be persisted by the caller.
     */
    suspend fun registerDevice(
        baseUrl: String,
        deviceName: String,
        deviceId: String,
    ): ApiResult<RegistrationResult> = withContext(Dispatchers.IO) {
        val capabilities = JSONObject().apply {
            put("supports_biometric", false)
            put("supports_push", true)
            put("biometric_types", JSONArray())
        }
        val deviceInfo = JSONObject().apply {
            put("device_id", deviceId)
            put("device_name", deviceName)
            put("platform", "android")
            put("capabilities", capabilities)
        }
        val body = JSONObject().apply { put("device_info", deviceInfo) }

        val request = Request.Builder()
            .url("$baseUrl/api/v1/mobile/register")
            .post(body.toString().toRequestBody(JSON_MEDIA.toMediaType()))
            .build()

        execute(request) { json ->
            RegistrationResult(
                apiKey = json.getString("api_key"),
                deviceId = json.optString("device_id", deviceId),
                expiresAt = json.optString("expires_at").takeIf { it.isNotBlank() },
            )
        }
    }

    /**
     * POST {base}/api/v1/mobile/web-session — returns the session token used to
     * authenticate the WebView. Requires the aim_ key.
     */
    suspend fun createWebSession(server: Server): ApiResult<String> =
        withContext(Dispatchers.IO) {
            val request = Request.Builder()
                .url("${server.baseUrl}/api/v1/mobile/web-session")
                .header("Authorization", "Bearer ${server.apiKey}")
                .post(ByteArray(0).toRequestBody(JSON_MEDIA.toMediaType()))
                .build()

            execute(request) { json -> json.getString("session_token") }
        }

    /**
     * POST {base}/api/v1/mobile/push/unifiedpush — associates a push endpoint
     * with this device on the given server.
     */
    suspend fun registerUnifiedPush(
        server: Server,
        endpoint: String,
    ): ApiResult<Unit> = withContext(Dispatchers.IO) {
        val body = JSONObject().apply {
            put("device_id", server.deviceId)
            put("endpoint", endpoint)
        }
        val request = Request.Builder()
            .url("${server.baseUrl}/api/v1/mobile/push/unifiedpush")
            .header("Authorization", "Bearer ${server.apiKey}")
            .post(body.toString().toRequestBody(JSON_MEDIA.toMediaType()))
            .build()

        execute(request) { }
    }

    private inline fun <T> execute(
        request: Request,
        parse: (JSONObject) -> T,
    ): ApiResult<T> {
        return try {
            client.newCall(request).execute().use { response ->
                val raw = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    return ApiResult.Failure(errorMessage(response.code, raw))
                }
                val json = if (raw.isBlank()) JSONObject() else JSONObject(raw)
                ApiResult.Success(parse(json))
            }
        } catch (e: IOException) {
            ApiResult.Failure("Network error: ${e.message ?: "unreachable"}")
        } catch (e: Exception) {
            ApiResult.Failure("Unexpected error: ${e.message ?: e.javaClass.simpleName}")
        }
    }

    private fun errorMessage(code: Int, raw: String): String {
        val detail = runCatching { JSONObject(raw).optString("detail") }
            .getOrNull()
            ?.takeIf { it.isNotBlank() }
        return if (detail != null) "$detail (HTTP $code)" else "Request failed (HTTP $code)"
    }
}
