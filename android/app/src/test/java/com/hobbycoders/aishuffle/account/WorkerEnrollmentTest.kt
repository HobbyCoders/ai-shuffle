package com.hobbycoders.aishuffle.account

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import java.io.IOException

class WorkerEnrollmentTest {
    @Test fun `worker proof rejection does not masquerade as an expired account session`() = runBlocking {
        val rejected = HubFailure(401, "Use a new pairing code.")
        val failure = runCatching { WorkerEnrollment.request("My worker") { throw rejected } }.exceptionOrNull()!!
        assertTrue(failure is WorkerEnrollmentFailure)
        assertFalse(failure is HubFailure) // NativeScreen clears account state only for HubFailure(401).
        assertSame(rejected, failure.cause)
        assertFalse(WorkerEnrollment.retryable(failure as Exception))
    }

    @Test fun `an unavailable new route can be retried and then enroll successfully`() = runBlocking {
        var attempts = 0
        suspend fun enroll(): String = WorkerEnrollment.request("My worker") {
            if (attempts++ == 0) throw HubFailure(502, "The host could not complete the request (502).")
            "accepted"
        }
        val first = runCatching { enroll() }.exceptionOrNull()!!
        assertTrue(WorkerEnrollment.retryable(first as Exception))
        assertTrue(first.message!!.contains("502"))
        assertEquals("accepted", enroll())
        assertEquals(2, attempts)
    }

    @Test fun `ambiguous enrollment conflict stops automatic retries without declaring account expired`() = runBlocking {
        val failure = runCatching {
            WorkerEnrollment.request("My worker") { throw HubFailure(409, "Ticket unavailable.") }
        }.exceptionOrNull()!!
        assertFalse(WorkerEnrollment.retryable(failure as Exception))
        assertTrue(failure.message!!.contains("Retry syncing."))
        assertTrue(failure.message!!.contains("sign out and sign in again"))
        assertFalse(failure is HubFailure)
    }

    @Test fun `cancellation is propagated and unexpected response data is not displayed`() = runBlocking {
        val cancelled = CancellationException("paused")
        assertSame(cancelled, runCatching { WorkerEnrollment.request("My worker") { throw cancelled } }.exceptionOrNull())
        val failure = runCatching {
            WorkerEnrollment.request("My worker") { throw IOException("secret-one-use-ticket") }
        }.exceptionOrNull()!!
        assertFalse(failure.message!!.contains("secret-one-use-ticket"))
        assertFalse(WorkerEnrollment.retryable(failure as Exception))
    }
}
