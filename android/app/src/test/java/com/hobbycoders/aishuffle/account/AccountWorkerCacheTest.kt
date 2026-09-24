package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server
import org.junit.Assert.*
import org.junit.Test

class AccountWorkerCacheTest {
    private val session = AccountSession("https://hub.example", "test-token", "2099-01-01T00:00:00Z", "device-one", "account-one", "tester")
    private val server = Server("account:worker-one", "Old name", "https://old-tunnel.example", "aim_test-key", session.deviceId,
        accountId = session.accountId, hubUrl = session.hub, hubServerId = "worker-one", cfAccessClientId = "origin-id", cfAccessClientSecret = "origin-secret")
    private fun update(value: Server = server, url: String = "https://new-tunnel.example") = AccountWorkerCache.updated(listOf(value), session, "worker-one", "New name", url)
    @Test fun registryCannotMoveAWorkerKeyOrAccessCredentialsToAnotherOrigin() {
        for (url in listOf("https://new-tunnel.example", "https://old-tunnel.example:8443", "https://sub.old-tunnel.example")) {
            val next = update(url = url)!!
            assertEquals(server.id, next.id); assertEquals(server.apiKey, next.apiKey); assertEquals(server.deviceId, next.deviceId)
            assertEquals(server.baseUrl, next.baseUrl); assertEquals("New name", next.name)
            assertEquals(server.cfAccessClientId, next.cfAccessClientId)
            assertEquals(server.cfAccessClientSecret, next.cfAccessClientSecret)
            assertEquals(server.baseUrl, AccountWorkerCache.listed(listOf(server), session, "worker-one", "New name", url).baseUrl)
        }
    }
    @Test fun equivalentHttpsSpellingsDoNotChangeThePinnedOrigin() {
        for (url in listOf("HTTPS://OLD-TUNNEL.EXAMPLE:443/", "https://old-tunnel.example/", server.baseUrl)) {
            val next = update(url = url)!!
            assertEquals(server.copy(name = "New name"), next)
        }
        val legacy = server.copy(baseUrl = "HTTPS://OLD-TUNNEL.EXAMPLE:443/")
        assertEquals(server.copy(name = "New name"), update(legacy)!!)
    }
    @Test fun registryCommitKeepsCurrentRepairOrRenewalInsteadOfStaleSnapshot() {
        val repaired = server.copy(apiKey = "aim_repaired", cfAccessClientSecret = "new-origin-secret")
        val stale = server.copy(name = "Renamed", baseUrl = "https://attacker.example")
        assertEquals(repaired.copy(name = "Renamed"), AccountWorkerCache.reconcile(listOf(repaired), listOf(stale)).single())
        // Even an independently saved connection at another origin cannot be
        // rolled back by a registry response captured before that explicit action.
        val independentlyPaired = repaired.copy(baseUrl = "https://independent.example", apiKey = "aim_new-pair")
        assertEquals(independentlyPaired.copy(name = "Renamed"), AccountWorkerCache.reconcile(listOf(independentlyPaired), listOf(stale)).single())
    }
    @Test fun staleRegistryCannotRestoreRemovedOrForeignDeviceCredentials() {
        for (saved in listOf(emptyList(), listOf(server.copy(deviceId = "other")), listOf(server.copy(accountId = "other")))) {
            val pending = AccountWorkerCache.reconcile(saved, listOf(server)).single()
            assertTrue(pending.needsAccountAuthorization)
            assertEquals("", pending.apiKey)
            assertNull(pending.keyEpoch); assertNull(pending.cfAccessClientId); assertNull(pending.cfAccessClientSecret)
        }
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
