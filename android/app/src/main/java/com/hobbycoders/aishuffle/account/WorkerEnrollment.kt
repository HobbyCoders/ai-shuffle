package com.hobbycoders.aishuffle.account

import kotlinx.coroutines.CancellationException
import java.io.IOException

/** A worker's rejected ticket must not invalidate the separate account session. */
class WorkerEnrollmentFailure(val status: Int, message: String, cause: Throwable) : IOException(message, cause)

internal object WorkerEnrollment {
    suspend fun <T> request(workerName: String, block: suspend () -> T): T = try {
        block()
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        // HubClient supplies fixed, safe error messages. Never display a raw
        // network/JSON exception, which may contain the ticket or worker response.
        val detail = when (e) {
            is HubFailure -> if (e.status == 409) {
                "The worker could not complete enrollment or already has an authorization for this phone. Retry syncing. If it still fails, sign out and sign in again to create a fresh phone identity."
            } else e.message
            else -> "The worker returned an unsupported response. Update the worker and retry."
        }
        throw WorkerEnrollmentFailure((e as? HubFailure)?.status ?: -1,
            "Could not finish authorizing $workerName. $detail", e)
    }

    fun retryable(error: Exception): Boolean {
        if (error is AccountWorkerSyncFailure) return error.retryable
        val status = when (error) {
            is WorkerEnrollmentFailure -> error.status
            is HubFailure -> error.status
            else -> return false
        }
        // A new tunnel may still be propagating while the worker is already
        // active in the account. Rejected identities/proofs need user action.
        return status in setOf(0, 404, 408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530)
    }
}
