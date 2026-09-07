package com.hobbycoders.aishuffle.ui

import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.util.concurrent.CancellationException

class ChatDownloadPolicyTest {
    @Test fun filenamesKeepUsefulExtensionButCannotSupplyPathsOrSpoofDirection() {
        assertEquals("picture.png", ChatDownloadPolicy.filename("../../picture.png"))
        assertEquals("picture.png", ChatDownloadPolicy.filename("C:\\temp\\picture.png"))
        assertEquals("report_.txt", ChatDownloadPolicy.filename("report\u202e.txt"))
        assertEquals("bad_name_.txt", ChatDownloadPolicy.filename("bad\u0000name?.txt"))
        assertEquals("download", ChatDownloadPolicy.filename(".."))
        val long = ChatDownloadPolicy.filename("a".repeat(200) + ".webp")
        assertEquals(120, long.length)
        assertTrue(long.endsWith(".webp"))
        assertEquals("image/png", ChatDownloadPolicy.mime("IMAGE/PNG; charset=utf-8"))
        assertEquals("application/octet-stream", ChatDownloadPolicy.mime("text/plain\r\nBad: header"))
    }
    @Test fun streamSupportsUnknownLengthAndEmptyFiles() {
        val sink = ByteArrayOutputStream()
        assertEquals(4, ChatDownloadPolicy.copy(ByteArrayInputStream("file".toByteArray()), sink))
        assertEquals("file", sink.toString("UTF-8"))
        assertEquals(0, ChatDownloadPolicy.copy(ByteArrayInputStream(byteArrayOf()), sink, 0))
    }
    @Test fun streamRejectsAnnouncedAndActualOversizeOrTruncation() {
        assertThrows(IllegalArgumentException::class.java) { ChatDownloadPolicy.copy(ByteArrayInputStream(byteArrayOf()), ByteArrayOutputStream(), 11, limit = 10) }
        assertThrows(IllegalArgumentException::class.java) { ChatDownloadPolicy.copy(ByteArrayInputStream(ByteArray(11)), ByteArrayOutputStream(), limit = 10) }
        assertThrows(IllegalArgumentException::class.java) { ChatDownloadPolicy.copy(ByteArrayInputStream(ByteArray(4)), ByteArrayOutputStream(), 5) }
    }
    @Test fun revokedOwnershipStopsBeforeBytesAreWritten() {
        val sink = ByteArrayOutputStream()
        var checks = 0
        assertThrows(CancellationException::class.java) {
            ChatDownloadPolicy.copy(ByteArrayInputStream("private".toByteArray()), sink, allowed = { ++checks < 2 })
        }
        assertEquals(0, sink.size())
    }
}
