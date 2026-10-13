package com.hobbycoders.aishuffle.presence

import org.junit.Assert.*
import org.junit.Test

class WorkerPresenceTest {
    @Test fun offlineRequiresTwoConsecutiveFailuresOnTheSameValidatedNetwork() {
        val status = WorkerPresence()
        status.record(ProbeResult.UNREACHABLE, 1_000, "wifi-one")
        assertEquals(PresenceState.UNKNOWN, status.state)
        status.record(ProbeResult.UNREACHABLE, 31_000, "wifi-one")
        assertEquals(PresenceState.OFFLINE, status.state)
        assertTrue(status.description("Home").contains("Not reachable from this phone"))
    }
    @Test fun absentValidationAndChangedNetworksCannotEstablishOffline() {
        val status = WorkerPresence()
        repeat(3) { status.record(ProbeResult.UNREACHABLE, it * 1_000L, null) }
        assertEquals(PresenceState.UNKNOWN, status.state)
        status.record(ProbeResult.UNREACHABLE, 4_000, "wifi-one")
        status.record(ProbeResult.UNREACHABLE, 5_000, "cellular")
        assertEquals(PresenceState.UNKNOWN, status.state)
    }
    @Test fun successfulLanProbeIsOnlineWithoutInternetValidation() {
        val status = WorkerPresence()
        status.record(ProbeResult.ONLINE, 0, null)
        assertEquals(PresenceState.ONLINE, status.state)
        status.record(ProbeResult.UNREACHABLE, 30_000, "wifi")
        assertEquals(PresenceState.UNKNOWN, status.state)
    }
    @Test fun serviceErrorsDoNotBecomeOfflineOrRetainGreen() {
        val status = WorkerPresence()
        status.record(ProbeResult.ONLINE, 0, "wifi")
        status.record(ProbeResult.UNAVAILABLE, 30_000, "wifi")
        assertEquals(PresenceState.UNAVAILABLE, status.state)
        status.record(ProbeResult.UNREACHABLE, 60_000, "wifi")
        assertEquals(PresenceState.UNKNOWN, status.state)
    }
    @Test fun staleObservationsAndFirstFailuresExpire() {
        val status = WorkerPresence()
        status.record(ProbeResult.ONLINE, 0, "wifi")
        status.expire(WorkerPresence.STALE_MS + 1)
        assertEquals(PresenceState.UNKNOWN, status.state)
        status.record(ProbeResult.UNREACHABLE, 100_000, "wifi")
        status.record(ProbeResult.UNREACHABLE, 200_001, "wifi")
        assertEquals(PresenceState.UNKNOWN, status.state)
    }
    @Test fun resetDropsOldWorkerAndNetworkEvidence() {
        val status = WorkerPresence()
        status.record(ProbeResult.UNREACHABLE, 0, "wifi")
        status.reset()
        status.record(ProbeResult.UNREACHABLE, 1_000, "wifi")
        assertEquals(PresenceState.UNKNOWN, status.state)
    }
}
