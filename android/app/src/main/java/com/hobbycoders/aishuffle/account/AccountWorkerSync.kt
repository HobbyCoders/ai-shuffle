package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.CancellationException
import java.io.IOException

internal class AccountWorkerSyncFailure(count: Int, val retryable: Boolean) : IOException(
    "$count worker connection${if (count == 1) " needs" else "s need"} authorization. Your workers are still listed. Tap a worker or Refresh to retry.")

/** One unavailable worker must not hide it or prevent another worker from loading. */
internal object AccountWorkerSync {
    suspend fun authorizePending(
        listed: List<Server>,
        accountMatches: () -> Boolean,
        save: (Server) -> Unit,
        enroll: suspend (Server) -> Server,
    ) {
        var failed = 0
        var retryable = true
        for (worker in listed.filter { it.needsAccountAuthorization }) {
            check(accountMatches()) { "Account changed. Sign in again." }
            try {
                val authorized = enroll(worker)
                check(accountMatches()) { "Account changed. Sign in again." }
                save(authorized)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                // Only a hub/session rejection logs the account out. Worker
                // rejections are wrapped by WorkerEnrollment and stay local.
                if (e is HubFailure && e.status == 401) throw e
                check(accountMatches()) { "Account changed. Sign in again." }
                failed++
                retryable = retryable && WorkerEnrollment.retryable(e)
            }
        }
        if (failed > 0) throw AccountWorkerSyncFailure(failed, retryable)
    }
}
