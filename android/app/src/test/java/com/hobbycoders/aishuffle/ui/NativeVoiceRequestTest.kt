package com.hobbycoders.aishuffle.ui

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class NativeVoiceRequestTest {
    private fun valid() = JSONObject().put("type", "start").put("id", "4165b4fc-c414-4e76-a39a-e173904831ca").put("sessionId", "existing_chat-123")
    @Test fun acceptsOnlyBoundedSelectedChatAndCorrelationId() {
        assertEquals("existing_chat-123", NativeVoiceRequest.parse(valid().toString()).sessionId)
        for (session in listOf("", "a".repeat(129), "../other", "https://other.example", "chat\nsecret")) {
            assertThrows(IllegalArgumentException::class.java) { NativeVoiceRequest.parse(valid().put("sessionId", session).toString()) }
        }
        assertThrows(IllegalArgumentException::class.java) { NativeVoiceRequest.parse(valid().put("id", "not-a-request-id").toString()) }
        assertThrows(IllegalArgumentException::class.java) { NativeVoiceRequest.parse(valid().put("type", "stop").toString()) }
    }
    @Test fun neverAcceptsCredentialOrOriginOverrides() {
        for (field in listOf("url", "serverId", "cookie", "apiKey")) {
            assertThrows(IllegalArgumentException::class.java) { NativeVoiceRequest.parse(valid().put(field, "untrusted").toString()) }
        }
        assertThrows(IllegalArgumentException::class.java) { NativeVoiceRequest.parse(" ".repeat(2049)) }
    }
}
