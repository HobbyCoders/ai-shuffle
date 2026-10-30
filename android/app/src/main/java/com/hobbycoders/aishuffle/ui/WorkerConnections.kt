package com.hobbycoders.aishuffle.ui

import android.content.Context
import android.os.SystemClock
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.presence.ProbeResult
import com.hobbycoders.aishuffle.presence.WorkerPresenceProbe
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withTimeoutOrNull

/** Only connection I/O and Android storage live here; the policy itself is JVM-testable. */
internal object WorkerConnections {
    private val policy = WorkerFailover()

    fun paired(context: Context): List<Server> {
        val store = ServerStore.get(context)
        val account = AccountStore(context).session()
        return store.orderedByRecentUse(store.getAll().filter {
            it.accountId == null || (it.accountId == account?.accountId && it.hubUrl == account?.hub && it.deviceId == account?.deviceId)
        })
    }

    /** Public, bounded, HTTPS probes; never enroll a worker or forward another worker's key. */
    suspend fun reachable(
        context: Context,
        failed: Server? = null,
        observations: Map<Server, ProbeResult>? = null,
    ): Server? = coroutineScope {
        val now = SystemClock.elapsedRealtime()
        if (failed != null) policy.failed(failed, now)
        val candidates = policy.candidates(paired(context).filter { ChatSessionOwner(context, it).current() }, now, failed)
        val results = candidates.map { candidate ->
            async {
                // The launcher already checks every visible row. Reuse that exact sweep,
                // never probe twice or reuse evidence from a changed pairing.
                val result = if (observations != null) observations[candidate] ?: ProbeResult.UNAVAILABLE
                    else withTimeoutOrNull(8_000) { WorkerPresenceProbe.check(candidate) } ?: ProbeResult.UNREACHABLE
                candidate to result
            }
        }.map { it.await() }
        val observedAt = SystemClock.elapsedRealtime()
        results.filter { it.second != ProbeResult.ONLINE }.forEach { policy.failed(it.first, observedAt) }
        // Pairings, credentials and accounts may have changed while the probes were in flight.
        val online = results.filter { it.second == ProbeResult.ONLINE }.map { it.first }
        val current = paired(context).filter { saved ->
            online.any { ChatSessionOwner.sameAuthorization(it, saved) } && ChatSessionOwner(context, saved).current()
        }
        policy.select(current, online.map { it.id }.toSet(), observedAt, failed)
    }
}
