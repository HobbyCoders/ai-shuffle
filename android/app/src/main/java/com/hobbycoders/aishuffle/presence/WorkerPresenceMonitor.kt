package com.hobbycoders.aishuffle.presence

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.SystemClock
import com.hobbycoders.aishuffle.account.AccountSession
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit

/** Owned by one visible worker screen. Cancellation invalidates even a late transport callback. */
internal class WorkerPresenceMonitor(
    private val context: Context,
    private val scope: CoroutineScope,
    private val publish: (Server, WorkerPresence) -> Unit,
) {
    private class Entry(val server: Server, val presence: WorkerPresence = WorkerPresence())
    internal data class NetworkState(val id: String?, val validated: Boolean)
    private val accountStore = AccountStore(context)
    private val serverStore = ServerStore.get(context)
    private var entries = emptyList<Entry>()
    private var account: AccountSession? = null
    private var polling: Job? = null
    private var aging: Job? = null
    private var generation = 0

    private fun network() = networkReader(context)

    private fun sameAccount(first: AccountSession?, second: AccountSession?) = if (first == null) second == null else
        second != null && first.hub == second.hub && first.accountId == second.accountId &&
            first.deviceId == second.deviceId && first.token == second.token

    private fun authorized(expected: AccountSession?) = if (expected == null)
        !accountStore.hasStoredSession() else accountStore.matches(expected)

    fun start(servers: List<Server>, session: AccountSession?) {
        cancel()
        val previous = entries.takeIf { sameAccount(account, session) }.orEmpty()
        entries = servers.map { server -> previous.firstOrNull { it.server == server } ?: Entry(server) }
        account = session
        val epoch = generation
        val current = entries
        current.forEach { it.presence.expire(SystemClock.elapsedRealtime()); publish(it.server, it.presence) }
        if (current.isEmpty()) return
        var lastNetwork = network()
        val semaphore = Semaphore(3)
        polling = scope.launch {
            while (isActive) {
                coroutineScope {
                    current.forEach { entry -> launch {
                        semaphore.withPermit {
                            if (epoch != generation || !authorized(session) || serverStore.getById(entry.server.id) != entry.server) return@withPermit
                            val before = network()
                            val result = WorkerPresenceProbe.check(entry.server)
                            if (epoch != generation || !authorized(session) || serverStore.getById(entry.server.id) != entry.server) return@withPermit
                            val after = network()
                            if (before.id != after.id) entry.presence.reset() else {
                                val stableValidatedNetwork = after.id.takeIf { before == after && after.validated }
                                // Success is evidence even on a LAN that Android cannot validate.
                                entry.presence.record(result, SystemClock.elapsedRealtime(), stableValidatedNetwork)
                            }
                            publish(entry.server, entry.presence)
                        }
                    } }
                }
                delay(30_000)
            }
        }
        aging = scope.launch {
            while (isActive) {
                delay(1_000)
                if (epoch != generation) return@launch
                val active = network()
                current.forEach { entry ->
                    if (!authorized(session) || serverStore.getById(entry.server.id) != entry.server || active != lastNetwork) entry.presence.reset()
                    else entry.presence.expire(SystemClock.elapsedRealtime())
                    publish(entry.server, entry.presence)
                }
                lastNetwork = active
            }
        }
    }

    private fun cancel() { generation++; polling?.cancel(); aging?.cancel(); polling = null; aging = null }
    fun stop() {
        cancel()
        // A hidden screen cannot observe network/account changes. Resume must establish new evidence.
        entries.forEach { it.presence.reset() }
    }

    companion object {
        private val networkReader: (Context) -> NetworkState = { context -> runCatching {
            val manager = context.getSystemService(ConnectivityManager::class.java)
            val active = manager.activeNetwork
            val capabilities = manager.getNetworkCapabilities(active)
            NetworkState(active?.toString(), capabilities?.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED) == true &&
                capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET))
        }.getOrDefault(NetworkState(null, false)) }
    }
}
