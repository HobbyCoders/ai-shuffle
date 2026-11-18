package com.hobbycoders.aishuffle.ui

import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest

/** Deterministic synthetic download bytes, generated on demand; nothing is held beyond the reader's buffer. */
internal class SyntheticFile(private val size: Long) : InputStream() {
    var position = 0L
        private set
    override fun read(): Int = if (position >= size) -1 else at(position++)
    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        if (position >= size) return -1
        val count = minOf(length.toLong(), size - position).toInt()
        for (i in 0 until count) buffer[offset + i] = at(position++).toByte()
        return count
    }

    companion object {
        /** The size of a real ZIP that the old 128 MB cap refused. */
        const val REPORTED_SIZE = 524_423_638L
        private fun at(index: Long) = ((index * 31) xor (index ushr 13)).toInt() and 0xFF
        fun digest(size: Long): ByteArray = DigestSink().also { SyntheticFile(size).copyTo(it, ChatDownloadPolicy.CHUNK_BYTES) }.digest.digest()
    }
}

/** Counts and hashes what a save wrote, and records its largest single write. */
internal class DigestSink : OutputStream() {
    val digest: MessageDigest = MessageDigest.getInstance("SHA-256")
    var count = 0L
        private set
    var largestWrite = 0
        private set
    override fun write(b: Int) { digest.update(b.toByte()); count++; largestWrite = maxOf(largestWrite, 1) }
    override fun write(b: ByteArray, off: Int, len: Int) {
        digest.update(b, off, len); count += len; largestWrite = maxOf(largestWrite, len)
    }
}
