package com.hobbycoders.aishuffle.model

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ServerSerializationTest {
    private val direct = Server("server-one", "Test server", "https://server.example.com", "aim_test-only", "device-one")
    private fun persisted(server: Server) = Server.fromJson(JSONObject(server.toJson().toString()))

    @Test fun directServerRemainsIdenticalAfterPersistence() {
        val loaded = persisted(direct)
        assertEquals(direct, loaded)
        assertNull(loaded.unifiedPushEndpoint)
        assertNull(loaded.cfAccessClientId)
        assertNull(loaded.cfAccessClientSecret)
        assertFalse(loaded.hasCfAccess)
    }
    @Test fun accountAndAccessCredentialsSurviveExactly() {
        val account = direct.copy(accountId = "account-one", hubUrl = "https://hub.example.com", hubServerId = "hub-server-one", cfAccessClientId = "test-id", cfAccessClientSecret = "test-secret")
        assertEquals(account, persisted(account))
        assertTrue(persisted(account).hasCfAccess)
    }
    @Test fun omittedLegacyFieldsAreNull() {
        val json = direct.toJson()
        listOf("accountId", "hubUrl", "hubServerId", "unifiedPushEndpoint", "cfAccessClientId", "cfAccessClientSecret").forEach { json.remove(it) }
        assertEquals(direct, Server.fromJson(json))
    }
    @Test fun explicitTextIsNotConfusedWithJsonNull() {
        assertEquals("null", persisted(direct.copy(cfAccessClientSecret = "null")).cfAccessClientSecret)
    }
    @Test fun staleOrModifiedAuthorizationStillFailsEquality() {
        val saved = persisted(direct)
        assertNotEquals(saved, direct.copy(apiKey = "aim_other"))
        assertNotEquals(saved, direct.copy(deviceId = "device-other"))
        assertNotEquals(saved, direct.copy(accountId = "account-other"))
        assertNotEquals(saved, direct.copy(baseUrl = "https://other.example.com"))
    }
}
