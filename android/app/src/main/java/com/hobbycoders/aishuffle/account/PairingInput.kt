package com.hobbycoders.aishuffle.account

import java.net.URI
import java.util.Locale

/** Only origins are accepted. A QR carries a short-lived code, never a credential. */
object PairingInput {
    data class Pair(val origin: String, val code: String)
    fun origin(raw: String): String {
        val u = try { URI(raw.trim()) } catch (_: Exception) { throw IllegalArgumentException("Enter a valid HTTPS address.") }
        require(u.scheme.equals("https", true) && !u.host.isNullOrBlank()) { "Use an HTTPS address with a host." }
        require(u.rawUserInfo == null && u.rawQuery == null && u.rawFragment == null && (u.rawPath.isNullOrEmpty() || u.rawPath == "/")) { "Enter only the HTTPS origin, without a path, password, query or fragment." }
        require(u.port == -1 || u.port in 1..65535) { "Invalid HTTPS port." }
        return "https://${u.host.lowercase(Locale.ROOT)}" + if (u.port == -1 || u.port == 443) "" else ":${u.port}"
    }
    fun code(raw: String): String {
        val value = raw.replace(" ", "").replace("-", "").uppercase(Locale.ROOT)
        require(value.matches(Regex("[A-Z0-9]{12}"))) { "Enter the 12-character pairing code. Spaces and hyphens are optional." }
        return value
    }
    fun parse(raw: String): Pair = parseFragment(raw, "code")
    fun accountQr(rawCode: String): String = "aishuffle://account/pair#code=${code(rawCode)}"
    fun parseAccount(raw: String, hub: String): Pair {
        val value = raw.trim()
        val prefix = "aishuffle://account/pair#code="
        if (value.startsWith(prefix)) return Pair(origin(hub), code(value.removePrefix(prefix)))
        // Accept existing QR codes only when they point to the configured Shuffle service.
        return parse(value).also { require(it.origin == origin(hub)) { "This QR is not a Shuffle account pairing code." } }
    }
    fun parseDirect(raw: String): Pair = parseFragment(raw, "token")
    fun workerCode(raw: String): String {
        val value = raw.trim()
        val prefix = "aishuffle://worker/link#code="
        val normalized = code(if (value.startsWith(prefix)) value.removePrefix(prefix) else value)
        require(normalized.matches(Regex("[A-Z2-7]{12}"))) { "Enter the worker’s 12-character account connection code." }
        return normalized
    }
    private fun parseFragment(raw: String, parameter: String): Pair {
        val u = try { URI(raw.trim()) } catch (_: Exception) { throw IllegalArgumentException("Scan a pairing QR or enter the address and code manually.") }
        require(u.rawPath == "/pair" && u.rawQuery == null && u.rawUserInfo == null) { "This is not the expected pairing QR. Use /pair#$parameter= followed by a pairing code." }
        val fragment = u.rawFragment.orEmpty()
        require(fragment.startsWith("$parameter=") && !fragment.contains('&')) { "The QR must contain only a pairing code." }
        return Pair(origin("${u.scheme}://${u.rawAuthority}"), code(fragment.removePrefix("$parameter=")))
    }
    fun sameOrigin(base: String, candidate: String): Boolean = runCatching {
        val u = URI(candidate)
        u.rawUserInfo == null && origin("${u.scheme}://${u.rawAuthority}") == origin(base)
    }.getOrDefault(false)
}
