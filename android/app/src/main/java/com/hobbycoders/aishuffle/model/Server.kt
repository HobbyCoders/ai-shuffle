package com.hobbycoders.aishuffle.model

import org.json.JSONObject
import java.util.UUID

/**
 * A self-hosted AI Shuffle server the user has connected to.
 *
 * @param id            stable local identifier for this entry
 * @param name          user-facing display name
 * @param baseUrl       normalized server origin (no trailing slash, http/https)
 * @param apiKey        the `aim_` device API key returned by /register
 * @param deviceId      uuid generated per-server and sent during registration
 * @param unifiedPushEndpoint last endpoint registered with this server, if any
 * @param cfAccessClientId     Cloudflare Access service-token Client ID, set only
 *                             when the server sits behind Cloudflare Access
 * @param cfAccessClientSecret Cloudflare Access service-token Client Secret
 */
data class Server(
    val id: String,
    val name: String,
    val baseUrl: String,
    val apiKey: String,
    val deviceId: String,
    val unifiedPushEndpoint: String? = null,
    val cfAccessClientId: String? = null,
    val cfAccessClientSecret: String? = null,
) {
    /** Host portion of [baseUrl], used as a compact subtitle in lists. */
    val host: String
        get() = baseUrl
            .substringAfter("://", baseUrl)
            .substringBefore('/')

    /** True when a Cloudflare Access service token is configured for this server. */
    val hasCfAccess: Boolean
        get() = !cfAccessClientId.isNullOrBlank() && !cfAccessClientSecret.isNullOrBlank()

    fun toJson(): JSONObject = JSONObject().apply {
        put("id", id)
        put("name", name)
        put("baseUrl", baseUrl)
        put("apiKey", apiKey)
        put("deviceId", deviceId)
        put("unifiedPushEndpoint", unifiedPushEndpoint ?: JSONObject.NULL)
        put("cfAccessClientId", cfAccessClientId ?: JSONObject.NULL)
        put("cfAccessClientSecret", cfAccessClientSecret ?: JSONObject.NULL)
    }

    companion object {
        fun fromJson(obj: JSONObject): Server = Server(
            id = obj.getString("id"),
            name = obj.getString("name"),
            baseUrl = obj.getString("baseUrl"),
            apiKey = obj.getString("apiKey"),
            deviceId = obj.getString("deviceId"),
            unifiedPushEndpoint = obj.optString("unifiedPushEndpoint", "")
                .takeIf { it.isNotBlank() },
            cfAccessClientId = obj.optString("cfAccessClientId", "")
                .takeIf { it.isNotBlank() },
            cfAccessClientSecret = obj.optString("cfAccessClientSecret", "")
                .takeIf { it.isNotBlank() },
        )

        /**
         * Build a new [Server] from raw user input.
         *
         * @throws IllegalArgumentException if the name is blank or the URL is not
         *         a valid http/https URL.
         */
        fun create(name: String, rawUrl: String, apiKey: String): Server {
            val cleanName = name.trim()
            require(cleanName.isNotEmpty()) { "Display name is required" }
            return Server(
                id = UUID.randomUUID().toString(),
                name = cleanName,
                baseUrl = normalizeUrl(rawUrl),
                apiKey = apiKey,
                deviceId = UUID.randomUUID().toString(),
            )
        }

        /** Trim, strip a trailing slash and require an http/https scheme. */
        fun normalizeUrl(rawUrl: String): String {
            val trimmed = rawUrl.trim().trimEnd('/')
            require(trimmed.isNotEmpty()) { "Server URL is required" }
            val lower = trimmed.lowercase()
            require(lower.startsWith("http://") || lower.startsWith("https://")) {
                "Server URL must start with http:// or https://"
            }
            // Reject a scheme with no host (e.g. "https://").
            require(trimmed.substringAfter("://").isNotBlank()) {
                "Server URL is missing a host"
            }
            return trimmed
        }
    }
}
