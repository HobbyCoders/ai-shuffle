package com.hobbycoders.aishuffle.net

import com.hobbycoders.aishuffle.model.Server
import okio.Buffer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class SessionPurposeTest {
    @Test fun webAndNativeVoiceMintIndependentSessionPurposes() {
        val server = Server.create("Fixture", "https://worker.example", "test-only")
        for (purpose in ApiClient.SessionPurpose.values()) {
            val request = ApiClient.webSessionRequest(server, purpose)
            val buffer = Buffer(); request.body!!.writeTo(buffer)
            assertEquals(purpose.value, JSONObject(buffer.readUtf8()).getString("purpose"))
            assertEquals("Bearer test-only", request.header("Authorization"))
        }
        val default = ApiClient.webSessionRequest(server)
        val buffer = Buffer(); default.body!!.writeTo(buffer)
        assertEquals("web", JSONObject(buffer.readUtf8()).getString("purpose"))
    }
}
