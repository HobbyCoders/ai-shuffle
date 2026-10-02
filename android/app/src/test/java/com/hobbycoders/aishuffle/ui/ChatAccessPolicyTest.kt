package com.hobbycoders.aishuffle.ui

import org.junit.Assert.*
import org.junit.Test

class ChatAccessPolicyTest {
    private val base = "https://worker.example:8443"
    private val page = "$base/mobile/chat/a"
    @Test fun onlyTopLevelWorkerMessagesCanSaveFiles() {
        assertTrue(ChatAccessPolicy.message(base, page, base, true, true))
        assertFalse(ChatAccessPolicy.message(base, page, base, false, true))
        assertFalse(ChatAccessPolicy.message(base, page, "https://other.example:8443", true, true))
        assertFalse(ChatAccessPolicy.message(base, "https://other.example", base, true, true))
        assertFalse(ChatAccessPolicy.message(base, page, base, true, false))
    }
    @Test fun microphoneNeverGrantsCameraMixedResourcesOrAnUntrustedOrigin() {
        val audio = arrayOf(ChatAccessPolicy.AUDIO_CAPTURE)
        assertTrue(ChatAccessPolicy.microphone(base, page, base, audio, true))
        for (origin in listOf("http://worker.example:8443", "https://worker.example", "https://user@worker.example:8443", "https://elsewhere.example")) {
            assertFalse(ChatAccessPolicy.microphone(base, page, origin, audio, true))
        }
        assertFalse(ChatAccessPolicy.microphone(base, page, base, emptyArray(), true))
        assertFalse(ChatAccessPolicy.microphone(base, page, base, arrayOf("android.webkit.resource.VIDEO_CAPTURE"), true))
        assertFalse(ChatAccessPolicy.microphone(base, page, base, audio + "android.webkit.resource.VIDEO_CAPTURE", true))
        assertFalse(ChatAccessPolicy.microphone(base, page, base, audio, false))
        assertFalse(ChatAccessPolicy.microphone(base, null, base, audio, true))
    }
    @Test fun fileChooserCannotReturnSelectionAfterAccountOrDocumentOriginChanges() {
        assertTrue(ChatAccessPolicy.document(base, page, true))
        assertFalse(ChatAccessPolicy.document(base, page, false))
        assertFalse(ChatAccessPolicy.document(base, "about:blank", true))
        assertFalse(ChatAccessPolicy.document(base, "https://worker.example.evil:8443/mobile/chat", true))
        // Another page on the paired worker (the desktop UI, an API response) gets no capability.
        assertFalse(ChatAccessPolicy.document(base, "$base/chat", true))
        assertFalse(ChatAccessPolicy.document(base, "$base/api/v1/auth/status", true))
    }
}
