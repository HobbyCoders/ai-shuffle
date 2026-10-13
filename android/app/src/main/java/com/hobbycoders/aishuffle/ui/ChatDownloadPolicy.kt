package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.account.PairingInput
import java.io.InputStream
import java.io.OutputStream
import java.net.URI
import java.net.URLDecoder
import java.net.URLEncoder
import java.util.concurrent.CancellationException

internal object ChatDownloadPolicy {
    const val MAX_BYTES = 128L * 1024 * 1024
    const val CHUNK_BYTES = 32 * 1024
    const val MAX_MESSAGE = 48 * 1024
    const val LIMIT_MESSAGE = "This file is too large. Shuffle can save files up to 128 MB."

    private fun workerUri(base: String, target: String): URI? = runCatching {
        require(target.length <= 8192 && PairingInput.sameOrigin(base, target))
        URI(target)
    }.getOrNull()

    // Asset/navigation admission is broader than download admission: root deep
    // links must reach ChatActivity's classifier, not the external-browser path.
    fun workerUrl(base: String, url: String): String? = url.takeIf { workerUri(base, it) != null }

    // The worker root is a UI document, not a file. This also protects the
    // download listener and file-save bridge if they bypass navigation handling.
    fun url(base: String, url: String): Boolean = workerUri(base, url)?.let {
        it.rawPath.orEmpty() !in listOf("", "/")
    } ?: false

    sealed class Navigation {
        data class Session(val url: String) : Navigation()
        object Download : Navigation()
        object Ignore : Navigation()
    }

    /** Same-origin navigation outside the phone UI; never grants credentials to another origin. */
    fun navigation(base: String, target: String, mainFrame: Boolean, gesture: Boolean): Navigation {
        val uri = workerUri(base, target) ?: return Navigation.Ignore
        if (uri.rawPath.orEmpty() in listOf("", "/")) {
            // Only an exact root path is a deep link; an empty path is ignored.
            if (!mainFrame || uri.rawPath != "/") return Navigation.Ignore
            val session = runCatching {
                uri.rawQuery.orEmpty().split('&').filter {
                    URLDecoder.decode(it.substringBefore('='), "UTF-8") == "session"
                }.singleOrNull()?.let { URLDecoder.decode(it.substringAfter('=', ""), "UTF-8") }
            }.getOrNull()
            if (session.isNullOrBlank() || session.any { it.isISOControl() || it == '\uFFFD' }) return Navigation.Ignore
            // Keep only the session, never a connect target, auth query or fragment.
            val encoded = URLEncoder.encode(session, "UTF-8").replace("+", "%20")
            val mobile = PairingInput.origin(base) + "/mobile/?session=" + encoded
            return if (mobile.length <= 8192) Navigation.Session(mobile) else Navigation.Ignore
        }
        return if (mainFrame && gesture) Navigation.Download else Navigation.Ignore
    }

    fun filename(raw: String?): String {
        val clean = raw.orEmpty().substringAfterLast('/').substringAfterLast('\\')
            .replace(Regex("[\\p{Cc}\\p{Cf}<>:\"|?*]"), "_").trim().trim('.')
            .ifBlank { "download" }
        if (clean.length <= 120) return clean
        val extension = clean.substringAfterLast('.', "").takeIf { it.length in 1..16 }?.let { ".$it" }.orEmpty()
        return clean.take(120 - extension.length) + extension
    }

    fun mime(raw: String?): String = raw.orEmpty().substringBefore(';').trim().lowercase()
        .takeIf { it.matches(Regex("[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+")) } ?: "application/octet-stream"

    fun length(size: Long) { require(size in 0..MAX_BYTES) { LIMIT_MESSAGE } }

    fun copy(input: InputStream, output: OutputStream, expected: Long = -1, allowed: () -> Boolean = { true }, limit: Long = MAX_BYTES): Long {
        require(expected <= limit) { LIMIT_MESSAGE }
        var count = 0L
        val buffer = ByteArray(CHUNK_BYTES)
        while (true) {
            if (!allowed()) throw CancellationException("Download cancelled")
            val size = input.read(buffer)
            if (size == -1) break
            count += size
            require(count <= limit) { LIMIT_MESSAGE }
            if (!allowed()) throw CancellationException("Download cancelled")
            output.write(buffer, 0, size)
        }
        require(expected == -1L || count == expected) { "The download was incomplete. Try again." }
        return count
    }
}
