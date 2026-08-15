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

/** A freshly created voice session on the server. */
data class VoiceSession(
    val sessionId: String,
    val clientSecret: String,
    val model: String,
)

/**
 * Client for the server's voice endpoints (under `/api/v1/voice`) and the
 * OpenAI Realtime SDP handshake.
 *
 * The voice endpoints do NOT accept the mobile `aim_` key — they require the
 * web `session` cookie minted by [ApiClient.createWebSession]. The OpenAI leg
 * uses only the short-lived `ek_` client secret the server returns; the real
 * OpenAI API key never reaches the device.
 */
object VoiceApi {

    private const val JSON_MEDIA = "application/json; charset=utf-8"
    private const val OPENAI_CALLS_URL = "https://api.openai.com/v1/realtime/calls"

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .build()
    }

    /** GET /api/v1/settings/voice — server-side voice configuration. */
    suspend fun isVoiceModeEnabled(server: Server, sessionCookie: String): ApiResult<Boolean> =
        withContext(Dispatchers.IO) {
            val request = serverRequest(server, sessionCookie, "/api/v1/settings/voice")
                .get()
                .build()
            execute(request) { json -> json.optBoolean("voice_mode_enabled", false) }
        }

    /** POST /api/v1/voice/sessions — creates a session and mints an `ek_` secret. */
    suspend fun createSession(server: Server, sessionCookie: String): ApiResult<VoiceSession> =
        withContext(Dispatchers.IO) {
            val request = serverRequest(server, sessionCookie, "/api/v1/voice/sessions")
                .post(JSONObject().toString().toRequestBody(JSON_MEDIA.toMediaType()))
                .build()
            execute(request) { json ->
                VoiceSession(
                    sessionId = json.getString("session_id"),
                    clientSecret = json.getString("client_secret"),
                    model = json.getString("model"),
                )
            }
        }

    /**
     * POST /api/v1/voice/sessions/{id}/call — registers the OpenAI `call_id` so
     * the server's sideband bridge joins the call, declaring [clientTools] (the
     * phone tools THIS app services) alongside its own agent-dispatch tools.
     *
     * [sessionMode] ("command" | "chat") tells the server which instruction
     * set to compose — the prompts themselves live in the server's voice
     * settings, editable in the AI Shuffle web UI, not in this app.
     */
    suspend fun attachCall(
        server: Server,
        sessionCookie: String,
        sessionId: String,
        callId: String,
        clientTools: JSONArray,
        sessionMode: String,
    ): ApiResult<Unit> = withContext(Dispatchers.IO) {
        val body = JSONObject().apply {
            put("call_id", callId)
            put("client_tools", clientTools)
            put("session_mode", sessionMode)
        }
        val request = serverRequest(server, sessionCookie, "/api/v1/voice/sessions/$sessionId/call")
            .post(body.toString().toRequestBody(JSON_MEDIA.toMediaType()))
            .build()
        execute(request) { }
    }

    /** POST /api/v1/voice/sessions/{id}/hangup — tears down the sideband. */
    suspend fun hangup(server: Server, sessionCookie: String, sessionId: String): ApiResult<Unit> =
        withContext(Dispatchers.IO) {
            val request = serverRequest(server, sessionCookie, "/api/v1/voice/sessions/$sessionId/hangup")
                .post(ByteArray(0).toRequestBody(JSON_MEDIA.toMediaType()))
                .build()
            execute(request) { }
        }

    /**
     * The OpenAI WebRTC handshake: POST the local SDP offer, get the answer SDP
     * back, and read the `call_id` from the `Location` header — exactly what the
     * web frontend does in `voiceCall.ts`.
     */
    suspend fun postSdpOffer(
        clientSecret: String,
        model: String,
        offerSdp: String,
    ): ApiResult<Pair<String, String?>> = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url("$OPENAI_CALLS_URL?model=$model")
            .header("Authorization", "Bearer $clientSecret")
            .post(offerSdp.toRequestBody("application/sdp".toMediaType()))
            .build()
        try {
            client.newCall(request).execute().use { response ->
                val raw = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    return@withContext ApiResult.Failure(
                        "OpenAI rejected the call (HTTP ${response.code}): ${raw.take(300)}",
                    )
                }
                val callId = response.header("Location")
                    ?.trimEnd('/')
                    ?.substringAfterLast('/')
                    ?.takeIf { it.isNotBlank() }
                ApiResult.Success(raw to callId)
            }
        } catch (e: IOException) {
            ApiResult.Failure("Could not reach OpenAI: ${e.message ?: "network error"}")
        }
    }

    /** A request builder for [server] carrying the session cookie + CF Access headers. */
    private fun serverRequest(server: Server, sessionCookie: String, path: String): Request.Builder {
        val builder = Request.Builder()
            .url("${server.baseUrl}$path")
            .header("Cookie", "session=$sessionCookie")
            // Cookie-authenticated POSTs pass through the server's CSRF
            // middleware, which requires an Origin matching the Host —
            // browsers send this automatically, OkHttp doesn't.
            .header("Origin", server.baseUrl)
        if (server.hasCfAccess) {
            builder.header("CF-Access-Client-Id", server.cfAccessClientId!!)
            builder.header("CF-Access-Client-Secret", server.cfAccessClientSecret!!)
        }
        return builder
    }

    private inline fun <T> execute(request: Request, parse: (JSONObject) -> T): ApiResult<T> {
        return try {
            client.newCall(request).execute().use { response ->
                val raw = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    val detail = runCatching { JSONObject(raw).optString("detail") }
                        .getOrNull()
                        ?.takeIf { it.isNotBlank() }
                    return ApiResult.Failure(
                        detail?.let { "$it (HTTP ${response.code})" }
                            ?: "Request failed (HTTP ${response.code})",
                    )
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
}
