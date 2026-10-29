package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.model.Server
import org.junit.Assert.*
import org.junit.Test

class WorkerFailoverTest {
    private val pc = Server("pc", "DesktopWorker", "https://pc.example", "aim_pc", "phone")
    private val nas = Server("nas", "ServerWorker", "https://nas.example", "aim_nas", "phone")
    private val laptop = Server("laptop", "Laptop", "https://laptop.example", "aim_laptop", "phone")
    private val recent = listOf(pc, nas, laptop)
    private val online = recent.map { it.id }.toSet()

    @Test fun startupKeepsTheLastUsedReachableWorker() {
        assertEquals(pc, WorkerFailover().select(recent, online, 0))
    }

    @Test fun startupSkipsOfflineWorkersInLastUsedOrderNotProbeCompletionOrder() {
        assertEquals(nas, WorkerFailover().select(recent, linkedSetOf(laptop.id, nas.id), 0))
    }

    @Test fun anAccountListingWithoutAPhoneKeyIsNotAuthorization() {
        val pending = pc.copy(apiKey = "", accountId = "account", hubUrl = "https://hub.example", hubServerId = "pc")
        assertEquals(nas, WorkerFailover().select(listOf(pending, nas), online, 0))
        assertNull(WorkerFailover().select(listOf(pending, nas.copy(apiKey = " ")), online, 0))
    }

    @Test fun credentialsOrInsecureAndMalformedOriginsAreNeverProbed() {
        val invalid = listOf("http://pc.example", "https://name:secret@pc.example", "https://pc.example/path", "https://pc.example?key=bad", "not a URL")
        for (url in invalid) assertTrue(url, WorkerFailover().candidates(listOf(pc.copy(baseUrl = url)), 0).isEmpty())
    }

    @Test fun noReachableWorkerLeavesTheExistingErrorAndSetupPathAvailable() {
        assertNull(WorkerFailover().select(recent, emptySet(), 0))
        assertNull(WorkerFailover().select(emptyList(), online, 0))
    }

    @Test fun failedBootstrapOrMainFrameMovesToTheNextAuthorizedWorker() {
        val policy = WorkerFailover()
        policy.failed(pc, 0)
        assertEquals(nas, policy.select(recent, online, 1, excluding = pc))
        // A failing replacement cannot immediately send us back to the worker we just left.
        policy.failed(nas, 2)
        assertEquals(laptop, policy.select(recent, online, 3, excluding = nas))
        policy.failed(laptop, 4)
        assertNull(policy.select(recent, online, 5, excluding = laptop))
    }

    @Test fun failureQuarantinesTheOriginNotJustOneSavedEntry() {
        val alias = pc.copy(id = "alias", baseUrl = "https://PC.example:443/")
        val policy = WorkerFailover()
        policy.failed(pc, 0)
        assertEquals(nas, policy.select(listOf(alias, nas), setOf(alias.id, nas.id), 1))
        assertEquals(listOf(pc, nas), WorkerFailover().candidates(listOf(pc, alias, nas), 0))
        assertEquals(nas, WorkerFailover().select(listOf(alias, nas), setOf(alias.id, nas.id), 0, excluding = pc))
    }

    @Test fun duplicateErrorsDoNotExtendCooldownAndRepeatedOutagesBackOffToFiveMinutes() {
        val policy = WorkerFailover()
        policy.failed(pc, 0)
        policy.failed(pc, 59_999)
        assertNull(policy.select(listOf(pc), online, 59_999))
        assertEquals(pc, policy.select(listOf(pc), online, 60_000))
        policy.failed(pc, 60_000)
        assertNull(policy.select(listOf(pc), online, 179_999))
        assertEquals(pc, policy.select(listOf(pc), online, 180_000))
        policy.failed(pc, 180_000)
        assertNull(policy.select(listOf(pc), online, 419_999))
        policy.failed(pc, 420_000)
        assertNull(policy.select(listOf(pc), online, 719_999))
        assertEquals(pc, policy.select(listOf(pc), online, 720_000))
        policy.failed(pc, 720_000)
        assertNull(policy.select(listOf(pc), online, 1_019_999))
        assertEquals(pc, policy.select(listOf(pc), online, 1_020_000))
    }

    @Test fun onlyTransportAndGatewayFailuresAutomaticallySwitchNotAuthOrOldWorkers() {
        for (status in listOf(null, 502, 503, 504, 521, 522, 523, 524, 530)) assertTrue(WorkerFailover.unreachableHttp(status))
        for (status in listOf(200, 301, 401, 403, 404, 409, 429, 500)) assertFalse(WorkerFailover.unreachableHttp(status))
    }
}
