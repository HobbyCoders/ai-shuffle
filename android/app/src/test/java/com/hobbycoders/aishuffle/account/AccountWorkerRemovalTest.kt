package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class AccountWorkerRemovalTest {
    private val session = AccountSession("https://hub.example", "test-token", "2099-01-01T00:00:00Z", "phone", "account-one", "tester")
    private val worker = AccountWorkerCache.listed(emptyList(), session, "old-worker", "Old worker", "https://old.example")

    @Test fun `account worker is revoked before forgetting its cached connection`() = runBlocking {
        val events = mutableListOf<String>()
        val pending = AccountWorkerRemoval.remove(worker, session, { true }, { events.add("forget:$it") }) { account, id ->
            assertEquals(session, account)
            events.add("revoke:$id")
            JSONObject().put("ok", true)
        }
        assertEquals(listOf("revoke:old-worker", "forget:${worker.id}"), events)
        assertFalse(pending)
    }

    @Test fun `authorized and authorization-pending workers use the same registry removal`() = runBlocking {
        for (server in listOf(worker, worker.copy(apiKey = "aim_cached-key"))) {
            var revoked = false
            var forgotten = false
            AccountWorkerRemoval.remove(server, session, { true }, { forgotten = true }) { _, id ->
                assertEquals(server.hubServerId, id)
                revoked = true
                JSONObject().put("ok", true)
            }
            assertTrue(revoked)
            assertTrue(forgotten)
        }
    }

    @Test fun `hub failure and cancellation keep the connection available for retry`() = runBlocking {
        for (failure in listOf(HubFailure(0, "Offline"), HubFailure(401, "Expired"),
            HubFailure(503, "Unavailable"), CancellationException("Cancelled"))) {
            var forgotten = false
            val error = runCatching {
                AccountWorkerRemoval.remove(worker, session, { true }, { forgotten = true }) { _, _ -> throw failure }
            }.exceptionOrNull()
            assertSame(failure, error)
            assertFalse(forgotten)
        }
    }

    @Test fun `wrong or signed-out account cannot revoke or forget an account worker`() = runBlocking {
        val otherAccount = AccountSession(session.hub, session.token, session.expiresAt, session.deviceId, "other-account", session.username)
        val otherHub = AccountSession("https://other.example", session.token, session.expiresAt, session.deviceId, session.accountId, session.username)
        for (account in listOf(null, otherAccount, otherHub)) {
            var touched = false
            val error = runCatching {
                AccountWorkerRemoval.remove(worker, account, { true }, { touched = true }) { _, _ ->
                    touched = true
                    JSONObject()
                }
            }.exceptionOrNull()
            assertTrue(error is IllegalArgumentException)
            assertFalse(touched)
        }
    }

    @Test fun `stale session is checked before and after the request`() = runBlocking {
        var called = false
        var forgotten = false
        val before = runCatching {
            AccountWorkerRemoval.remove(worker, session, { false }, { forgotten = true }) { _, _ ->
                called = true
                JSONObject()
            }
        }.exceptionOrNull()
        assertTrue(before is IllegalStateException)
        assertFalse(called)
        var matches = true
        val during = runCatching {
            AccountWorkerRemoval.remove(worker, session, { matches }, { forgotten = true }) { _, _ ->
                matches = false
                JSONObject().put("ok", true)
            }
        }.exceptionOrNull()
        assertTrue(during is IllegalStateException)
        assertFalse(forgotten)
    }

    @Test fun `missing or unsafe registry identity cannot delete a different resource`() = runBlocking {
        for (id in listOf(null, "", "../other", "worker/tickets")) {
            var touched = false
            val error = runCatching {
                AccountWorkerRemoval.remove(worker.copy(hubServerId = id), session, { true }, { touched = true }) { _, _ ->
                    touched = true
                    JSONObject()
                }
            }.exceptionOrNull()
            assertTrue(error is IllegalArgumentException)
            assertFalse(touched)
        }
    }

    @Test fun `remote cleanup pending still forgets revoked worker and reports pending cleanup`() = runBlocking {
        var forgotten = false
        val pending = AccountWorkerRemoval.remove(worker, session, { true }, { forgotten = true }) { _, _ ->
            JSONObject().put("ok", true).put("cleanup_pending", true)
        }
        assertTrue(forgotten)
        assertTrue(pending)
    }

    @Test fun `direct pairings can be removed offline without changing the account`() = runBlocking {
        val direct = Server("direct", "Direct", "https://direct.example", "aim_direct-key", "local-phone")
        for (account in listOf(null, session)) {
            val forgotten = mutableListOf<String>()
            val pending = AccountWorkerRemoval.remove(direct, account, { error("No account check for direct pairings") }, { forgotten.add(it) }) { _, _ ->
                error("Direct pairing removal must not contact the hub")
            }
            assertEquals(listOf("direct"), forgotten)
            assertFalse(pending)
        }
    }
}
