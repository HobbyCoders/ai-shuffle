package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class AccountWorkerSyncTest {
    private val session = AccountSession("https://hub.example", "test-token", "2099-01-01T00:00:00Z", "phone-new", "account-one", "tester")
    private fun pending(id: String) = AccountWorkerCache.listed(emptyList(), session, id, id, "https://$id.example")

    @Test fun `signing in again keeps registry workers visible without reusing old phone authority`() {
        val old = pending("gaming-pc").copy(apiKey = "aim_old-key", deviceId = "phone-old")
        val current = AccountWorkerCache.listed(listOf(old), session, "gaming-pc", "Gaming PC", "https://gaming-pc.example")
        assertEquals("Gaming PC", current.name)
        assertEquals(session.deviceId, current.deviceId)
        assertTrue(current.needsAccountAuthorization)
        assertEquals("", current.apiKey)
        assertNull(current.cfAccessClientSecret)
    }

    @Test fun `failed worker remains listed and does not prevent later workers authorizing`() = runBlocking {
        val listed = listOf(pending("offline"), pending("online"))
        val saved = listed.associateBy { it.id }.toMutableMap()
        val error = runCatching {
            AccountWorkerSync.authorizePending(listed, { true }, { saved[it.id] = it }) {
                if (it.name == "offline") throw WorkerEnrollmentFailure(503, "Unavailable", Exception())
                it.copy(apiKey = "aim_new-key")
            }
        }.exceptionOrNull()
        assertNotNull(error)
        assertEquals(2, saved.size)
        assertTrue(saved[listed[0].id]!!.needsAccountAuthorization)
        assertFalse(saved[listed[1].id]!!.needsAccountAuthorization)
        assertTrue(error!!.message!!.contains("still listed"))
        assertFalse(error is HubFailure)
        assertTrue(WorkerEnrollment.retryable(error as Exception))
    }

    @Test fun `rejected worker identity stays visible without an automatic retry loop`() = runBlocking {
        val waiting = pending("worker")
        val error = runCatching {
            AccountWorkerSync.authorizePending(listOf(waiting), { true }, { fail("Must not save") }) {
                throw WorkerEnrollmentFailure(401, "Rejected identity", Exception())
            }
        }.exceptionOrNull()
        assertTrue(error is AccountWorkerSyncFailure)
        assertFalse(WorkerEnrollment.retryable(error as Exception))
        assertTrue(waiting.needsAccountAuthorization)
    }

    @Test fun `refresh authorizes only pending workers and preserves cached keys`() = runBlocking {
        val cached = pending("cached").copy(apiKey = "aim_cached")
        val waiting = pending("waiting")
        val attempts = mutableListOf<String>()
        val saved = mutableListOf<Server>()
        AccountWorkerSync.authorizePending(listOf(cached, waiting), { true }, { saved += it }) {
            attempts += it.id
            it.copy(apiKey = "aim_ready")
        }
        assertEquals(listOf(waiting.id), attempts)
        assertEquals("aim_ready", saved.single().apiKey)
        assertEquals("aim_cached", cached.apiKey)
    }

    @Test fun `account rejection and cancellation retain their original meaning`() = runBlocking {
        for (failure in listOf(HubFailure(401, "Expired"), CancellationException("Paused"))) {
            var attempts = 0
            val actual = runCatching {
                AccountWorkerSync.authorizePending(listOf(pending("first"), pending("second")), { true }, { fail("Must not save") }) {
                    attempts++
                    throw failure
                }
            }.exceptionOrNull()
            assertSame(failure, actual)
            assertEquals(1, attempts)
        }
    }

    @Test fun `account change during enrollment cannot save a previous account credential`() = runBlocking {
        var sameAccount = true
        val error = runCatching {
            AccountWorkerSync.authorizePending(listOf(pending("worker")), { sameAccount }, { fail("Must not save") }) {
                sameAccount = false
                it.copy(apiKey = "aim_previous-account")
            }
        }.exceptionOrNull()
        assertTrue(error is IllegalStateException)
        assertEquals("Account changed. Sign in again.", error!!.message)
    }
}
