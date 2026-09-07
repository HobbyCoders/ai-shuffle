package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server
import org.junit.Assert.*
import org.junit.Test

class AccountWorkerCacheTest {
    private val session = AccountSession("https://hub.example", "test-token", "2099-01-01T00:00:00Z", "device-one", "account-one", "tester")
    private val server = Server("account:worker-one", "Old name", "https://old-tunnel.example", "aim_test-key", session.deviceId,
        accountId = session.accountId, hubUrl = session.hub, hubServerId = "worker-one", cfAccessClientId = "origin-id", cfAccessClientSecret = "origin-secret")
    private fun update(value: Server = server, url: String = "https://new-tunnel.example") = AccountWorkerCache.updated(listOf(value), session, "worker-one", "New name", url)
    @Test fun trustedRegistryTunnelChangePreservesWorkerDeviceKeyWithoutReenrollment() {
        val next = update()!!
        assertEquals(server.id, next.id); assertEquals(server.apiKey, next.apiKey); assertEquals(server.deviceId, next.deviceId)
        assertEquals("https://new-tunnel.example", next.baseUrl); assertEquals("New name", next.name)
        assertNull(next.cfAccessClientId); assertNull(next.cfAccessClientSecret)
        assertEquals("origin-secret", update(url = server.baseUrl)!!.cfAccessClientSecret)
    }
    @Test fun foreignHubAccountWorkerAndDeviceCannotReuseTheCredential() {
        assertNull(update(server.copy(hubUrl = "https://foreign.example")))
        assertNull(update(server.copy(accountId = "other-account")))
        assertNull(update(server.copy(hubServerId = "other-worker")))
        assertNull(update(server.copy(deviceId = "other-device")))
        assertNull(update(server.copy(apiKey = "legacy-key")))
        assertNull(update(server.copy(apiKey = "aim_")))
    }
    @Test fun registryCannotRedirectCredentialsToHttpOrAnOriginWithUserInfo() {
        for (url in listOf("http://new-tunnel.example", "https://user@new-tunnel.example", "https://new-tunnel.example/path", "file:///worker")) {
            assertThrows(IllegalArgumentException::class.java) { update(url = url) }
        }
    }
}
