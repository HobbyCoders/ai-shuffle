package com.hobbycoders.aishuffle.ui

import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.util.concurrent.CancellationException

class ChatDownloadPolicyTest {
    private val base = "https://worker.example"
    private fun navigation(target: String, mainFrame: Boolean = true, gesture: Boolean = true) =
        ChatDownloadPolicy.navigation(base, target, mainFrame, gesture)

    @Test fun navigationAdmissionIncludesRootDeepLinksButDownloadAdmissionDoesNot() {
        for (target in listOf("$base/?session=parent", "$base/?session=", "$base/", "$base?session=parent")) {
            assertEquals(target, ChatDownloadPolicy.workerUrl(base, target))
            assertFalse(ChatDownloadPolicy.url(base, target))
        }
        val admitted = ChatDownloadPolicy.workerUrl(base, "$base/?session=parent")!!
        assertEquals(ChatDownloadPolicy.Navigation.Session("$base/mobile/?session=parent"), navigation(admitted))
        for (target in listOf("https://outside.example/?session=parent", "http://worker.example/a",
            "https://user@worker.example/a", "https://worker.example:444/a", "$base/" + "a".repeat(8192))) {
            assertNull(target, ChatDownloadPolicy.workerUrl(base, target))
        }
    }

    @Test fun rootSessionLinksLoadThePhoneUiWithoutRequiringAGesture() {
        for (gesture in listOf(true, false)) {
            assertEquals(ChatDownloadPolicy.Navigation.Session("$base/mobile/?session=parent"),
                navigation("$base/?session=parent", gesture = gesture))
        }
        assertEquals(ChatDownloadPolicy.Navigation.Session("$base/mobile/?session=parent"),
            navigation("https://WORKER.example:443/?session=parent"))
    }

    @Test fun sessionQueriesAreDecodedOnceAndReencodedWithoutCopyingOtherParameters() {
        assertEquals(ChatDownloadPolicy.Navigation.Session("$base/mobile/?session=parent%20%2F%20chat%26%2B%3D%3F%23%E9%9B%AA"),
            navigation("$base/?view=code&%73ession=parent+%2F+chat%26%2B%3D%3F%23%E9%9B%AA&connect=https%3A%2F%2Fother.example#old"))
        assertEquals(ChatDownloadPolicy.Navigation.Session("$base/mobile/?session=%252F"),
            navigation("$base/?session=%252F"))
    }

    @Test fun rootDocumentsAndMalformedSessionQueriesNeverBecomeDownloads() {
        for (suffix in listOf("/", "/?other=value", "/?session", "/?session=", "/?session=+",
            "/?session=a&session=b", "/?session=a&%73ession=b", "/?session=%", "/?session=%FF",
            "/?session=%00", "/?session=%0D%0A", "/?session=" + "a".repeat(8192))) {
            assertEquals(suffix, ChatDownloadPolicy.Navigation.Ignore, navigation(base + suffix))
        }
        assertEquals(ChatDownloadPolicy.Navigation.Ignore, navigation("$base?session=parent"))
    }

    @Test fun sessionNavigationCannotEscapeTheOriginOrNavigateAFrame() {
        for (target in listOf("https://outside.example/?session=parent", "http://worker.example/?session=parent",
            "https://worker.example:444/?session=parent", "https://user@worker.example/?session=parent",
            "javascript:alert(1)", "/?session=parent")) {
            assertEquals(target, ChatDownloadPolicy.Navigation.Ignore, navigation(target))
        }
        for (gesture in listOf(true, false)) {
            assertEquals(ChatDownloadPolicy.Navigation.Ignore,
                navigation("$base/?session=parent", mainFrame = false, gesture = gesture))
        }
        // Raw paths are exact: an encoded slash or nested file path is not a session route.
        assertEquals(ChatDownloadPolicy.Navigation.Download, navigation("$base/%2F?session=parent"))
        assertEquals(ChatDownloadPolicy.Navigation.Download, navigation("$base/api/file?session=parent"))
    }

    @Test fun rootSessionLinksAreAlsoRejectedByTheDownloadListenerAndFileBridge() {
        assertFalse(ChatDownloadPolicy.url(base, "$base/?session=parent"))
        assertFalse(ChatDownloadPolicy.url(base, "$base/?session="))
        assertFalse(ChatDownloadPolicy.url(base, "$base/?%73ession=parent"))
        assertFalse(ChatDownloadPolicy.url(base, "$base?session=parent"))
        assertTrue(ChatDownloadPolicy.url(base, "$base/api/v1/files/download?path=report.pdf"))
        assertFalse(ChatDownloadPolicy.url(base, "https://outside.example/report.pdf"))
    }

    @Test fun genuineWorkerFilesStillRequireAMainFrameGesture() {
        for (target in listOf("$base/api/v1/files/download?path=a%20b.txt", "$base/attachments/report.pdf")) {
            assertEquals(ChatDownloadPolicy.Navigation.Download, navigation(target))
            assertEquals(ChatDownloadPolicy.Navigation.Ignore, navigation(target, gesture = false))
            assertEquals(ChatDownloadPolicy.Navigation.Ignore, navigation(target, mainFrame = false))
        }
    }

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
