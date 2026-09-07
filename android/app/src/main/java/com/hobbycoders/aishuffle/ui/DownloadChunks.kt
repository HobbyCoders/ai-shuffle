package com.hobbycoders.aishuffle.ui

import java.io.OutputStream

/** Sequential acknowledged chunks; completion must match the declared length exactly. */
internal class DownloadChunks(private val size: Long) {
    private var count = 0L
    private var next = 0
    init { ChatDownloadPolicy.length(size) }
    fun write(sequence: Int, bytes: ByteArray, output: OutputStream) {
        require(sequence == next)
        require(bytes.isNotEmpty() && bytes.size <= ChatDownloadPolicy.CHUNK_BYTES && count + bytes.size <= size)
        output.write(bytes)
        count += bytes.size
        next++
    }
    fun finish(sequence: Int) { require(sequence == next && count == size) }
}
