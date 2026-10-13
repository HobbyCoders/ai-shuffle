package com.hobbycoders.aishuffle.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class WebUserAgentTest {
    private val webView = "Mozilla/5.0 (Linux; Android 15; Pixel 9 Build/AP4A) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/140.0.0.0 Mobile Safari/537.36"

    @Test fun appendsTheAppTokenAfterTheDefaultUserAgent() {
        val decorated = WebUserAgent.decorate(webView, "0.8.2")
        assertEquals("$webView AIShuffleAndroid/0.8.2", decorated)
        assertTrue(decorated.startsWith(webView))
    }

    @Test fun appendsOnlyOnce() {
        val once = WebUserAgent.decorate(webView, "0.8.2")
        assertEquals(once, WebUserAgent.decorate(once, "0.8.2"))
        assertEquals(once, WebUserAgent.decorate(once, "0.9.0"))
    }

    @Test fun toleratesMissingBaseAndVersion() {
        assertEquals("AIShuffleAndroid/0", WebUserAgent.decorate(null, null))
        assertEquals("AIShuffleAndroid/0", WebUserAgent.decorate("  ", ""))
        assertEquals("AIShuffleAndroid/1.0-beta", WebUserAgent.token(" 1.0 beta "))
    }

    @Test fun tokenMatchesTheFrontendPattern() {
        // frontend/src/lib/utils/platform.ts matches /\bAIShuffleAndroid\//.
        val pattern = Regex("\\bAIShuffleAndroid/")
        assertTrue(pattern.containsMatchIn(WebUserAgent.decorate(webView, "0.8.2")))
        assertTrue(!pattern.containsMatchIn(webView))
    }
}
