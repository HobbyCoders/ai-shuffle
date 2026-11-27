package com.hobbycoders.aishuffle.ui

import org.junit.Assert.*
import org.junit.Test

class ChatLinkMenuTest {
    @Test fun webLinksAreOfferedExactlyAsWebViewReportsThem() {
        for (url in listOf("https://example.com/docs?page=2#top", "http://example.com/", "HTTPS://Example.com/a%20b",
            "https://worker.example/api/v1/files/report.pdf")) {
            assertEquals(url, ChatLinkMenu.copyableLink(url))
        }
    }

    @Test fun scriptDataAppAndEmptyLinksAreNotOffered() {
        for (url in listOf(null, "", "javascript:alert(1)", "data:text/html,hi", "file:///sdcard/a.txt",
            "content://media/1", "intent://scan#Intent;end", "mailto:someone@example.com", "https://", "http:/example.com",
            "/relative/path", "https://example.com/" + "a".repeat(8192))) {
            assertNull(url, ChatLinkMenu.copyableLink(url))
        }
    }
}
