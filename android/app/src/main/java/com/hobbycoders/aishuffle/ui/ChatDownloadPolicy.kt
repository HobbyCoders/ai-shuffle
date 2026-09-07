package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.account.PairingInput
import java.io.InputStream
import java.io.OutputStream
import java.util.concurrent.CancellationException

internal object ChatDownloadPolicy {
    const val MAX_BYTES = 128L * 1024 * 1024
    const val CHUNK_BYTES = 32 * 1024
    const val MAX_MESSAGE = 48 * 1024
    const val LIMIT_MESSAGE = "This file is too large. Shuffle can save files up to 128 MB."

    fun url(base: String, url: String): Boolean = url.length <= 8192 && PairingInput.sameOrigin(base, url)

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
