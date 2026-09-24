package com.hobbycoders.aishuffle.net

import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.account.PairingInput
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.time.Instant
import java.util.concurrent.TimeUnit

/** Result of an [ApiClient] call. Never throws to the caller. */
sealed class ApiResult<out T> {
    data class Success<T>(val data: T) : ApiResult<T>()
    data class Failure(val message: String, val statusCode: Int? = null) : ApiResult<Nothing>()

    /**
     * The server is behind Cloudflare Access and no (valid) service token was
     * supplied. The UI should collect a CF Access service token and retry.
     */
    object NeedsCfAccess : ApiResult<Nothing>()
}

/** Data returned by the device-registration endpoint. */
data class RegistrationResult(
    val apiKey: String,
    val deviceId: String,
    val expiresAt: String?,
)

internal sealed class WebSessionReservation {
    class Ready(val token: String, val expiresAt: Instant, val deadlineMillis: Long) : WebSessionReservation()
    object Unsupported : WebSessionReservation()
}

/**
 * Thin OkHttp wrapper around the AI Shuffle mobile API. Every call is suspending
 * and runs on [Dispatchers.IO]; callers must never touch the main thread for
 * networking.
 *
 * Redirects are intentionally **not** followed: a server fronted by Cloudflare
 * Access answers unauthenticated requests with a 302 to its login page, and we
 * want to detect that (and ask for a service token) rather than silently fetch
 * an HTML login page and fail to parse it as JSON.
 */
object ApiClient {

    private const val JSON_MEDIA = "application/json; charset=utf-8"

    @Volatile private var deviceKeys: DeviceKeyRenewal? = null

    internal fun installCredentialStore(read: (String) -> Server?, replace: (Server, String) -> Server?) {
        deviceKeys = DeviceKeyRenewal(read, replace, ::renewDeviceKey)
    }

    /** All saved-device producers use this, including tools and native voice. */
    internal suspend fun currentServer(server: Server): ApiResult<Server> = deviceKeys?.prepare(server)
        ?: ApiResult.Failure("Open the app to unlock its saved worker credentials.")

    private suspend fun <T> withCurrentServer(server: Server, call: suspend (Server) -> ApiResult<T>): ApiResult<T> =
        when (val current = currentServer(server)) {
            is ApiResult.Success -> call(current.data)
            is ApiResult.Failure -> current
            ApiResult.NeedsCfAccess -> ApiResult.NeedsCfAccess
        }

    internal fun keyRenewalRequest(server: Server): Request {
        require(PairingInput.origin(server.baseUrl) == server.baseUrl)
        return Request.Builder().url("${server.baseUrl}/api/v1/mobile/key/renew")
            .header("Authorization", "Bearer ${server.apiKey}")
            .header("Accept", "application/json").header("Cache-Control", "no-store")
            .cfAccess(server.cfAccessClientId, server.cfAccessClientSecret)
            .post("{}".toRequestBody(JSON_MEDIA.toMediaType())).build()
    }

    internal fun parseKeyRenewal(server: Server, code: Int, contentType: String?, raw: String): ApiResult<RenewedDeviceKey> {
        if (raw.toByteArray(Charsets.UTF_8).size > 8192)
            return ApiResult.Failure("The worker returned an invalid key renewal.", code)
        val json = if (contentType?.substringBefore(';')?.trim()?.equals("application/json", true) == true)
            runCatching { JSONObject(raw) }.getOrNull() else null
        // Compatibility is positively identified, never inferred from HTML,
        // authorization failure, an outage, or a redirect to an untrusted origin.
        if ((code == 404 && json?.opt("detail") == "Not Found") ||
            (code == 405 && json?.opt("detail") == "Method Not Allowed"))
            return ApiResult.Success(RenewedDeviceKey(server.apiKey))
        if (code != 200) return ApiResult.Failure(errorMessage(code, raw, server.deviceId), code)
        return try {
            require(json != null && json.opt("device_id") == server.deviceId)
            val key = json.opt("api_key") as? String ?: error("Missing key")
            val rotated = json.opt("rotated") as? Boolean ?: error("Missing rotation status")
            require(if (rotated) Regex("aim_[A-Za-z0-9_-]{43}").matches(key) else key == server.apiKey)
            Instant.parse(json.opt("expires_at") as? String ?: error("Missing expiry"))
            ApiResult.Success(RenewedDeviceKey(key))
        } catch (_: Exception) { ApiResult.Failure("The worker returned an invalid key renewal.", code) }
    }

    private suspend fun renewDeviceKey(server: Server): ApiResult<RenewedDeviceKey> = withContext(Dispatchers.IO) {
        try {
            client.newCall(keyRenewalRequest(server)).execute().use { response ->
                if (isCfAccessChallenge(response)) return@withContext ApiResult.NeedsCfAccess
                val source = response.body?.source()
                    ?: return@withContext ApiResult.Failure("The worker returned an invalid key renewal.", response.code)
                source.request(8193)
                if (source.buffer.size > 8192) return@withContext ApiResult.Failure("The worker returned an invalid key renewal.", response.code)
                parseKeyRenewal(server, response.code, response.header("Content-Type"), source.buffer.readUtf8())
            }
        } catch (_: IOException) { ApiResult.Failure("Cannot renew this worker's credential while it is unreachable. Your saved credential was kept; retry connecting.") }
        catch (_: Exception) { ApiResult.Failure("The worker returned an invalid key renewal.") }
    }

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .callTimeout(45, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .build()
    }

    /**
     * POST {base}/api/v1/mobile/register — no app auth header.
     *
     * If the server is behind Cloudflare Access, pass a service token via
     * [cfAccessClientId] / [cfAccessClientSecret]; omit them on the first attempt
     * so a [ApiResult.NeedsCfAccess] can be surfaced when Access is detected.
     *
     * The returned api_key is shown only once and must be persisted by the caller.
     */
    suspend fun registerDevice(
        baseUrl: String,
        deviceName: String,
        deviceId: String,
        cfAccessClientId: String? = null,
        cfAccessClientSecret: String? = null,
        pairingCode: String,
    ): ApiResult<RegistrationResult> = withContext(Dispatchers.IO) {
        val capabilities = JSONObject().apply {
            put("supports_biometric", false)
            put("supports_push", false)
            put("biometric_types", JSONArray())
        }
        val deviceInfo = JSONObject().apply {
            put("device_id", deviceId)
            put("device_name", deviceName)
            put("platform", "android")
            put("capabilities", capabilities)
        }
        val body = JSONObject().apply {
            put("device_info", deviceInfo)
            put("pairing_code", com.hobbycoders.aishuffle.account.PairingInput.code(pairingCode))
        }

        val request = Request.Builder()
            .url("$baseUrl/api/v1/mobile/register")
            .cfAccess(cfAccessClientId, cfAccessClientSecret)
            .post(body.toString().toRequestBody(JSON_MEDIA.toMediaType()))
            .build()

        execute(request, hasCfCreds = hasCf(cfAccessClientId, cfAccessClientSecret)) { json ->
            RegistrationResult(
                apiKey = json.getString("api_key"),
                deviceId = json.optString("device_id", deviceId),
                expiresAt = json.optString("expires_at").takeIf { it.isNotBlank() },
            )
        }
    }

    /**
     * POST {base}/api/v1/mobile/web-session — returns the session token used to
     * authenticate the WebView. Requires the aim_ key (and the CF Access service
     * token, if the server is behind Access).
     */
    enum class SessionPurpose(val value: String) { WEB("web"), NATIVE("native") }

    internal const val WEB_RESERVATION_HEADER = "X-Shuffle-Web-Session-Reservation"
    private val reservationFormat = Regex("wsm_[A-Za-z0-9_-]{43}")

    internal fun webSessionRequest(server: Server, purpose: SessionPurpose = SessionPurpose.WEB, reservation: String? = null): Request {
        require(reservation == null || purpose == SessionPurpose.WEB && reservationFormat.matches(reservation))
        return Request.Builder().url("${server.baseUrl}/api/v1/mobile/web-session")
            .header("Authorization", "Bearer ${server.apiKey}")
            .cfAccess(server.cfAccessClientId, server.cfAccessClientSecret)
            .apply { reservation?.let { header(WEB_RESERVATION_HEADER, it) } }
            .post(JSONObject().put("purpose", purpose.value).toString().toRequestBody(JSON_MEDIA.toMediaType())).build()
    }

    internal fun webReservationRequest(server: Server): Request {
        require(PairingInput.origin(server.baseUrl) == server.baseUrl)
        return Request.Builder().url("${server.baseUrl}/api/v1/mobile/web-session/reservations")
            .header("Authorization", "Bearer ${server.apiKey}")
            .cfAccess(server.cfAccessClientId, server.cfAccessClientSecret)
            .header("Cache-Control", "no-store")
            .post("{}".toRequestBody(JSON_MEDIA.toMediaType())).build()
    }

    internal fun monotonicMillis(): Long = TimeUnit.NANOSECONDS.toMillis(System.nanoTime())

    internal fun parseWebReservation(code: Int, contentType: String?, raw: String,
        startedAt: Long = monotonicMillis(), receivedAt: Long = monotonicMillis(), deviceId: String? = null): ApiResult<WebSessionReservation> {
        if (raw.toByteArray(Charsets.UTF_8).size > 8192) return ApiResult.Failure("The worker returned an invalid session reservation.", code)
        val json = if (contentType?.substringBefore(';')?.trim()?.equals("application/json", true) == true)
            runCatching { JSONObject(raw) }.getOrNull() else null
        if ((code == 404 && json?.opt("detail") == "Not Found") ||
            (code == 405 && json?.opt("detail") == "Method Not Allowed")) return ApiResult.Success(WebSessionReservation.Unsupported)
        if (code != 200) return ApiResult.Failure(errorMessage(code, raw, deviceId), code)
        return try {
            val token = json?.opt("reservation") as? String ?: error("Missing reservation")
            val expiry = Instant.parse(json.opt("expires_at") as? String ?: error("Missing expiry"))
            val lifetime = json.opt("expires_in")
            require(reservationFormat.matches(token) && lifetime is Int && lifetime in 1..120)
            val deadline = startedAt + lifetime * 1000L
            require(receivedAt < deadline)
            ApiResult.Success(WebSessionReservation.Ready(token, expiry, deadline))
        } catch (_: Exception) { ApiResult.Failure("The worker returned an invalid session reservation.", code) }
    }

    internal suspend fun reserveWebSession(server: Server): ApiResult<WebSessionReservation> = withCurrentServer(server) { current -> withContext(Dispatchers.IO) {
        val startedAt = monotonicMillis()
        try {
            client.newCall(webReservationRequest(current)).execute().use { response ->
                if (isCfAccessChallenge(response)) return@withContext ApiResult.NeedsCfAccess
                val source = response.body?.source() ?: return@withContext ApiResult.Failure("The worker returned an invalid session reservation.", response.code)
                source.request(8193)
                if (source.buffer.size > 8192) return@withContext ApiResult.Failure("The worker returned an invalid session reservation.", response.code)
                parseWebReservation(response.code, response.header("Content-Type"), source.buffer.readUtf8(), startedAt, deviceId = current.deviceId)
            }
        } catch (_: IOException) { ApiResult.Failure("Cannot reach this server. Check your HTTPS address and connection, then retry.") }
        catch (_: Exception) { ApiResult.Failure("The worker returned an invalid session reservation.") }
    } }

    internal suspend fun commitWebSession(server: Server, reservation: WebSessionReservation.Ready): ApiResult<String> = withCurrentServer(server) { current -> withContext(Dispatchers.IO) {
        execute(webSessionRequest(current, SessionPurpose.WEB, reservation.token), hasCfCreds = current.hasCfAccess, deviceId = current.deviceId) { it.getString("session_token") }
    } }

    suspend fun createWebSession(server: Server, purpose: SessionPurpose = SessionPurpose.WEB): ApiResult<String> =
        withCurrentServer(server) { current -> withContext(Dispatchers.IO) {
            execute(webSessionRequest(current, purpose), hasCfCreds = current.hasCfAccess, deviceId = current.deviceId) { json ->
                json.getString("session_token")
            }
        } }

    /**
     * POST {base}/api/v1/mobile/push/unifiedpush — associates a push endpoint
     * with this device on the given server.
     */
    suspend fun registerUnifiedPush(
        server: Server,
        endpoint: String,
    ): ApiResult<Unit> = withCurrentServer(server) { current -> withContext(Dispatchers.IO) {
        val body = JSONObject().apply {
            put("device_id", current.deviceId)
            put("endpoint", endpoint)
        }
        val request = Request.Builder()
            .url("${current.baseUrl}/api/v1/mobile/push/unifiedpush")
            .header("Authorization", "Bearer ${current.apiKey}")
            .cfAccess(current.cfAccessClientId, current.cfAccessClientSecret)
            .post(body.toString().toRequestBody(JSON_MEDIA.toMediaType()))
            .build()

        execute(request, hasCfCreds = current.hasCfAccess, deviceId = current.deviceId) { }
    } }

    /**
     * Exchanges the server's Cloudflare Access service token for a
     * `CF_Authorization` cookie so the WebView (and its WebSocket) can be
     * authorized against Access. Returns the raw `CF_Authorization=<jwt>` cookie
     * pair to seed into the WebView's CookieManager, or null if the server isn't
     * behind Access or Access didn't issue a cookie.
     */
    suspend fun fetchCfAuthorizationCookie(server: Server): String? =
        withContext(Dispatchers.IO) {
            if (!server.hasCfAccess) return@withContext null
            val request = Request.Builder()
                .url("${server.baseUrl}/")
                .cfAccess(server.cfAccessClientId, server.cfAccessClientSecret)
                .get()
                .build()
            runCatching {
                client.newCall(request).execute().use { response ->
                    response.headers("Set-Cookie")
                        .firstOrNull { it.startsWith("CF_Authorization=", ignoreCase = true) }
                        ?.substringBefore(';')
                }
            }.getOrNull()
        }

    /** Attach Cloudflare Access service-token headers when both parts are present. */
    private fun Request.Builder.cfAccess(clientId: String?, clientSecret: String?): Request.Builder {
        if (hasCf(clientId, clientSecret)) {
            header("CF-Access-Client-Id", clientId!!)
            header("CF-Access-Client-Secret", clientSecret!!)
        }
        return this
    }

    private fun hasCf(clientId: String?, clientSecret: String?): Boolean =
        !clientId.isNullOrBlank() && !clientSecret.isNullOrBlank()

    /**
     * True when [response] is a Cloudflare Access auth challenge: a redirect to
     * the Access login, or an explicit `WWW-Authenticate: Cloudflare-Access`.
     */
    private fun isCfAccessChallenge(response: Response): Boolean {
        if (response.isRedirect) {
            val location = response.header("Location").orEmpty()
            if (location.contains(".cloudflareaccess.com", ignoreCase = true) ||
                location.contains("/cdn-cgi/access/", ignoreCase = true)
            ) {
                return true
            }
        }
        return response.header("WWW-Authenticate")
            .orEmpty()
            .startsWith("Cloudflare-Access", ignoreCase = true)
    }

    private inline fun <T> execute(
        request: Request,
        hasCfCreds: Boolean,
        deviceId: String? = null,
        parse: (JSONObject) -> T,
    ): ApiResult<T> {
        return try {
            client.newCall(request).execute().use { response ->
                if (isCfAccessChallenge(response)) {
                    // A token was supplied but Access still refused it → bad token.
                    return if (hasCfCreds) {
                        ApiResult.Failure(
                            "Cloudflare Access rejected the service token. " +
                                "Double-check the Client ID and Secret and that a " +
                                "policy allows this token.",
                        )
                    } else {
                        ApiResult.NeedsCfAccess
                    }
                }

                val raw = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    return ApiResult.Failure(errorMessage(response.code, raw, deviceId), response.code)
                }
                // A 200 carrying HTML means a proxy/login page slipped through.
                if (raw.trimStart().startsWith("<")) {
                    return ApiResult.Failure(
                        "The server returned a web page instead of data. It may be " +
                            "behind a login or proxy.",
                    )
                }
                val json = if (raw.isBlank()) JSONObject() else JSONObject(raw)
                ApiResult.Success(parse(json))
            }
        } catch (e: IOException) {
            ApiResult.Failure("Cannot reach this server. Check your HTTPS address and connection, then retry.")
        } catch (e: Exception) {
            ApiResult.Failure("The server returned an unsupported response. Update the server and retry.")
        }
    }

    internal fun errorMessage(code: Int, raw: String, deviceId: String? = null): String {
        val reason = runCatching { JSONObject(raw).optString("detail") }.getOrNull()
        if (code == 403 && reason == "Confirm this device on the worker before connecting") {
            // This is our saved enrollment identity, never an ID or label echoed
            // by the hub/worker error body. Account enrollment IDs are UUIDs.
            val ownId = deviceId?.takeIf { it.matches(Regex("[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")) }
            if (ownId == null) return "Waiting for worker confirmation. This phone's saved device ID is unavailable; do not approve by name. Reopen this worker and retry."
            return "Waiting for worker confirmation. Phone device ID: $ownId. Compare this exact ID on the worker before approving; a matching name is not proof. Ask the owner to approve this phone on the worker, then retry. Your saved credential has been kept."
        }
        if (code == 403 && reason == "This device was denied by the worker owner")
            return "The worker owner denied this phone. Contact the owner before enrolling a new device identity."
        return when (code) {
            401 -> "Authorization expired or was revoked. Pair this phone again."
            403 -> "Pairing was refused. Check the code and this device’s access."
            404, 405 -> "This server does not support this mobile action. Check the address and update the server."
            409, 410 -> "This pairing code was used or expired. Generate a new code."
            else -> "The server could not complete this request (HTTP $code). Try again."
        }
    }
}
