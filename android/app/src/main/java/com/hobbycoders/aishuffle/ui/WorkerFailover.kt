package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.model.Server

/** Pure connection policy. Callers supply saved, account-scoped workers in last-used order. */
internal class WorkerFailover {
    private data class Failure(val attempts: Int, val retryAt: Long)
    private val failures = mutableMapOf<String, Failure>()

    /** A process-wide caller keeps this history across ChatActivity hand-offs. */
    fun failed(worker: Server, now: Long) {
        val origin = origin(worker) ?: return
        val previous = failures[origin]
        // Duplicate bootstrap/WebView errors must not extend the same outage forever.
        if (previous != null && now < previous.retryAt) return
        val attempts = ((previous?.attempts ?: 0) + 1).coerceAtMost(4)
        val delay = (60_000L * (1L shl (attempts - 1))).coerceAtMost(300_000L)
        failures[origin] = Failure(attempts, now + delay)
    }

    /** No credentials, insecure origins, duplicate origins, or recently failed workers. */
    fun candidates(ordered: List<Server>, now: Long, excluding: Server? = null): List<Server> {
        val excluded = excluding?.let(::origin)
        return ordered.filter { worker ->
            val origin = origin(worker)
            worker.apiKey.isNotBlank() && origin != null && origin != excluded &&
                now >= (failures[origin]?.retryAt ?: Long.MIN_VALUE)
        }.distinctBy { origin(it) }
    }

    /** Probe completion order never overrides the owner's last-used order. */
    fun select(ordered: List<Server>, online: Set<String>, now: Long, excluding: Server? = null): Server? =
        candidates(ordered, now, excluding).firstOrNull { it.id in online }

    private fun origin(worker: Server): String? = runCatching { PairingInput.origin(worker.baseUrl) }.getOrNull()

    companion object {
        /** Tunnel/gateway failures also mean an offline worker; auth/version errors do not. */
        fun unreachableHttp(status: Int?): Boolean = status == null || status in setOf(502, 503, 504, 521, 522, 523, 524, 530)
    }
}
