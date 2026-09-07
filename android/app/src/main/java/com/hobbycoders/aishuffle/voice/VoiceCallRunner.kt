package com.hobbycoders.aishuffle.voice

import com.hobbycoders.aishuffle.net.VoiceSession
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.first
import org.json.JSONObject

internal enum class VoiceMediaState { CONNECTING, READY, DISCONNECTED, FAILED, CLOSED }

/** Injectable media boundary: tests never load native WebRTC or open a microphone. */
internal interface VoicePeer {
    val mediaState: StateFlow<VoiceMediaState>
    suspend fun createOffer(): String
    suspend fun acceptAnswer(answerSdp: String)
    fun setMicEnabled(enabled: Boolean)
    fun send(event: JSONObject)
    fun close()
}

internal interface VoiceBackend {
    suspend fun open(sessionId: String?): VoiceSession
    suspend fun exchange(session: VoiceSession, offer: String): Pair<String, String?>
    suspend fun attach(sessionId: String, callId: String)
    suspend fun checkAccess()
}

/** One owner and one microphone. Refresh always reuses the exact session. */
internal class VoiceCallRunner(
    private val backend: VoiceBackend,
    private val newPeer: () -> VoicePeer,
    private val authorized: () -> Boolean,
    private val onSession: (String) -> Unit,
    private val onPeer: (VoicePeer?) -> Unit,
    private val onStatus: (CallStatus) -> Unit,
    private val setupTimeoutMs: Long = 90_000,
    private val disconnectGraceMs: Long = 12_000,
    private val refreshAfterMs: Long = 50 * 60_000,
    private val accessCheckMs: Long = 30_000,
) {
    private fun requireAuthorized() {
        check(authorized()) { "Access to this worker changed. Reconnect from Shuffle." }
    }

    suspend fun run(selectedSession: String?) = coroutineScope {
        require(selectedSession == null || validVoiceSessionId(selectedSession)) { "Invalid voice session." }
        var sessionId = selectedSession
        var firstConnection = true
        val accessJob = launch {
            var elapsed = 0L
            while (isActive) {
                val interval = minOf(1_000L, accessCheckMs)
                delay(interval)
                requireAuthorized()
                elapsed += interval
                if (elapsed >= accessCheckMs) {
                    backend.checkAccess()
                    elapsed = 0
                }
            }
        }
        try {
            while (isActive) {
                requireAuthorized()
                onStatus(if (firstConnection) CallStatus.CONNECTING else CallStatus.RECONNECTING)
                firstConnection = false
                var peer: VoicePeer? = null
                try {
                    withTimeout(setupTimeoutMs) {
                        val session = backend.open(sessionId)
                        // Never fall back to a newly created session for a handoff or refresh.
                        check(sessionId == null || session.sessionId == sessionId) { "The worker returned a different voice session." }
                        requireAuthorized()
                        sessionId = session.sessionId
                        onSession(session.sessionId)
                        peer = newPeer()
                        onPeer(peer)
                        val active = checkNotNull(peer)
                        val (answer, callId) = backend.exchange(session, active.createOffer())
                        requireAuthorized()
                        check(!callId.isNullOrBlank()) { "The voice service did not identify this call. Please retry." }
                        active.acceptAnswer(answer)
                        val state = active.mediaState.first { it == VoiceMediaState.READY || it == VoiceMediaState.FAILED || it == VoiceMediaState.CLOSED }
                        check(state == VoiceMediaState.READY) { "The voice media connection could not open." }
                        backend.attach(session.sessionId, callId)
                        requireAuthorized()
                        check(active.mediaState.value == VoiceMediaState.READY) { "The voice connection changed during setup." }
                    }
                    onStatus(CallStatus.LIVE)
                    val active = checkNotNull(peer)
                    // No unbounded retry loop: one scheduled refresh, same session, then
                    // stop with a visible error if that refresh or its attachment fails.
                    withTimeoutOrNull(refreshAfterMs) {
                        while (isActive) {
                            val state = active.mediaState.first { it != VoiceMediaState.READY }
                            check(state == VoiceMediaState.DISCONNECTED || state == VoiceMediaState.CONNECTING) { "The voice connection closed." }
                            onStatus(CallStatus.RECONNECTING)
                            val recovered = withTimeoutOrNull(disconnectGraceMs) {
                                active.mediaState.first { it == VoiceMediaState.READY || it == VoiceMediaState.FAILED || it == VoiceMediaState.CLOSED }
                            }
                            check(recovered == VoiceMediaState.READY) { "The voice connection dropped. Start a new call when the network returns." }
                            requireAuthorized()
                            onStatus(CallStatus.LIVE)
                        }
                    }
                } finally {
                    onPeer(null)
                    peer?.close()
                }
                onStatus(CallStatus.RECONNECTING)
            }
        } finally {
            accessJob.cancel()
        }
    }
}

internal fun validVoiceSessionId(value: String): Boolean = value.matches(Regex("[A-Za-z0-9_-]{1,128}"))
