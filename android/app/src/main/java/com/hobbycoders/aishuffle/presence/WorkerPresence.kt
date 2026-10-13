package com.hobbycoders.aishuffle.presence

internal enum class ProbeResult { ONLINE, UNREACHABLE, UNAVAILABLE }
internal enum class PresenceState(val label: String) { ONLINE("Online"), OFFLINE("Offline"), UNKNOWN("Unknown"), UNAVAILABLE("Status unavailable") }

/** Ephemeral observations only: registration and cached credentials never imply availability. */
internal class WorkerPresence {
    var state = PresenceState.UNKNOWN
        private set
    private var observedAt: Long? = null
    private var failedAt: Long? = null
    private var failedNetwork: String? = null

    fun record(result: ProbeResult, now: Long, validatedNetwork: String?) {
        val consecutiveFailure = failedAt?.let { now >= it && now - it <= STALE_MS } == true &&
            validatedNetwork != null && validatedNetwork == failedNetwork
        state = when {
            result == ProbeResult.ONLINE -> PresenceState.ONLINE
            result == ProbeResult.UNREACHABLE && consecutiveFailure -> PresenceState.OFFLINE
            result == ProbeResult.UNREACHABLE -> PresenceState.UNKNOWN
            else -> PresenceState.UNAVAILABLE
        }
        observedAt = now
        failedAt = now.takeIf { result == ProbeResult.UNREACHABLE && validatedNetwork != null }
        failedNetwork = validatedNetwork.takeIf { failedAt != null }
    }

    fun expire(now: Long) {
        if (observedAt?.let { now < it || now - it > STALE_MS } == true) reset()
    }

    fun reset() { state = PresenceState.UNKNOWN; observedAt = null; failedAt = null; failedNetwork = null }

    fun description(workerName: String) = "$workerName. ${state.label}. " + when (state) {
        PresenceState.ONLINE -> "This worker responded to this phone."
        PresenceState.OFFLINE -> "Not reachable from this phone."
        PresenceState.UNKNOWN -> "Reachability has not been confirmed recently."
        PresenceState.UNAVAILABLE -> "The worker's status could not be verified."
    }

    companion object { const val STALE_MS = 90_000L }
}
