package com.hobbycoders.aishuffle.voice

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/** Where the call is in its lifecycle. */
enum class CallStatus { IDLE, CONNECTING, LIVE, ENDED, ERROR }

/** Snapshot of the live call for the UI. */
data class CallUiState(
    val status: CallStatus = CallStatus.IDLE,
    val serverName: String? = null,
    val userCaption: String = "",
    val assistantCaption: String = "",
    val muted: Boolean = false,
    val error: String? = null,
)

/**
 * Single source of truth shared between [VoiceCallService] (writer) and
 * [VoiceCallActivity] / the wake-word service (readers). A process-wide object
 * keeps this trivial — there is at most one call at a time.
 */
object VoiceCallState {
    private val _state = MutableStateFlow(CallUiState())
    val state: StateFlow<CallUiState> = _state

    val isActive: Boolean
        get() = _state.value.status == CallStatus.CONNECTING || _state.value.status == CallStatus.LIVE

    internal fun update(transform: (CallUiState) -> CallUiState) {
        _state.value = transform(_state.value)
    }

    internal fun reset() {
        _state.value = CallUiState()
    }
}
