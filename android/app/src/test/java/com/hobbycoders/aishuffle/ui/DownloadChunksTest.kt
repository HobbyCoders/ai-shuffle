package com.hobbycoders.aishuffle.ui

import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayOutputStream

class DownloadChunksTest {
    @Test fun validTransferPreservesBytesAndEmptyFileCanComplete() {
        val chunks = DownloadChunks(5); val out = ByteArrayOutputStream()
        chunks.write(0, "abc".toByteArray(), out); chunks.write(1, "de".toByteArray(), out); chunks.finish(2)
        assertEquals("abcde", out.toString("UTF-8"))
        DownloadChunks(0).finish(0)
    }
    @Test fun duplicateReorderedOversizedAndIncompleteChunksAreRejected() {
        val chunks = DownloadChunks(5); val out = ByteArrayOutputStream()
        assertThrows(IllegalArgumentException::class.java) { chunks.write(1, byteArrayOf(1), out) }
        chunks.write(0, "abc".toByteArray(), out)
        assertThrows(IllegalArgumentException::class.java) { chunks.write(0, byteArrayOf(1), out) }
        assertThrows(IllegalArgumentException::class.java) { chunks.write(1, byteArrayOf(), out) }
        assertThrows(IllegalArgumentException::class.java) { chunks.write(1, "def".toByteArray(), out) }
        assertThrows(IllegalArgumentException::class.java) { chunks.finish(1) }
        assertEquals("abc", out.toString("UTF-8"))
        assertThrows(IllegalArgumentException::class.java) { DownloadChunks(ChatDownloadPolicy.MAX_BYTES + 1) }
        assertThrows(IllegalArgumentException::class.java) { DownloadChunks(100_000).write(0, ByteArray(ChatDownloadPolicy.CHUNK_BYTES + 1), out) }
    }
}
