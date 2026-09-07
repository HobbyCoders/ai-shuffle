package com.hobbycoders.aishuffle.voice

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.util.Log
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONObject
import org.webrtc.AudioSource
import org.webrtc.AudioTrack
import org.webrtc.DataChannel
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.audio.JavaAudioDeviceModule
import java.nio.ByteBuffer
import java.nio.charset.StandardCharsets
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * The WebRTC leg of an OpenAI Realtime call, mirroring the web frontend's
 * `voiceCall.ts`: one mic track up, one audio track down (played automatically
 * by the audio device module), and an `oai-events` data channel carrying the
 * realtime JSON events both ways.
 *
 * Usage: [createOffer] → POST the SDP via [com.hobbycoders.aishuffle.net.VoiceApi.postSdpOffer]
 * → [acceptAnswer] → exchange events → [close].
 */
internal class RealtimeClient(
    context: Context,
    private val onEvent: (JSONObject) -> Unit,
) : VoicePeer {

    private val state = MutableStateFlow(VoiceMediaState.CONNECTING)
    override val mediaState: StateFlow<VoiceMediaState> = state
    @Volatile private var peerState = PeerConnection.PeerConnectionState.NEW
    @Volatile private var channelOpen = false

    private fun publishState() {
        state.value = when {
            closed || peerState == PeerConnection.PeerConnectionState.CLOSED -> VoiceMediaState.CLOSED
            peerState == PeerConnection.PeerConnectionState.FAILED -> VoiceMediaState.FAILED
            peerState == PeerConnection.PeerConnectionState.DISCONNECTED -> VoiceMediaState.DISCONNECTED
            peerState == PeerConnection.PeerConnectionState.CONNECTED && channelOpen -> VoiceMediaState.READY
            else -> VoiceMediaState.CONNECTING
        }
    }

    private val appContext = context.applicationContext
    private val audioManager = appContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    private val factory: PeerConnectionFactory
    private val audioSource: AudioSource
    private val micTrack: AudioTrack
    private val peer: PeerConnection
    private val events: DataChannel

    @Volatile
    private var closed = false

    init {
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(appContext)
                .createInitializationOptions(),
        )
        val audioModule = JavaAudioDeviceModule.builder(appContext)
            .setUseHardwareAcousticEchoCanceler(true)
            .setUseHardwareNoiseSuppressor(true)
            .createAudioDeviceModule()
        factory = try {
            PeerConnectionFactory.builder().setAudioDeviceModule(audioModule).createPeerConnectionFactory()
        } finally { audioModule.release() }

        val constraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("googEchoCancellation", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googNoiseSuppression", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googAutoGainControl", "true"))
        }
        audioSource = factory.createAudioSource(constraints)
        micTrack = factory.createAudioTrack("mic0", audioSource)

        val config = PeerConnection.RTCConfiguration(emptyList()).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
        }
        peer = factory.createPeerConnection(config, PeerObserver()) ?: run {
            micTrack.dispose()
            audioSource.dispose()
            factory.dispose()
            throw IllegalStateException("Could not create the WebRTC peer connection")
        }

        // The events channel must exist BEFORE the offer so it is negotiated in
        // the same exchange (no second round trip).
        events = peer.createDataChannel("oai-events", DataChannel.Init())
        events.registerObserver(object : DataChannel.Observer {
            override fun onBufferedAmountChange(previousAmount: Long) {}
            override fun onStateChange() {
                channelOpen = events.state() == DataChannel.State.OPEN
                if (events.state() == DataChannel.State.CLOSED) state.value = VoiceMediaState.CLOSED
                else publishState()
            }
            override fun onMessage(buffer: DataChannel.Buffer) {
                val bytes = ByteArray(buffer.data.remaining())
                buffer.data.get(bytes)
                val text = String(bytes, StandardCharsets.UTF_8)
                runCatching { JSONObject(text) }
                    .onSuccess(onEvent)
                    .onFailure { Log.w(TAG, "Unparsable realtime event") }
            }
        })

        peer.addTrack(micTrack, listOf("mic-stream"))

        configureAudioRouting()
    }

    /**
     * Route call audio to whatever the user is already wearing. In
     * MODE_IN_COMMUNICATION Android does NOT follow media routing on its own —
     * a connected headset must be explicitly selected as the communication
     * device or audio falls out of the phone. Speaker is only the fallback
     * when nothing external is available.
     */
    private fun configureAudioRouting() {
        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
            val devices = audioManager.availableCommunicationDevices
            val preferred = devices.firstOrNull { it.type in EXTERNAL_TYPES }
                ?: devices.firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
            if (preferred != null) {
                val ok = audioManager.setCommunicationDevice(preferred)
                Log.i(TAG, "Comm device -> ${preferred.type} (ok=$ok)")
            }
        } else {
            val hasBt = audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
                .any {
                    it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                        it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
                }
            val hasWired = audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
                .any { it.type in EXTERNAL_TYPES && it.type != AudioDeviceInfo.TYPE_BLUETOOTH_SCO }
            @Suppress("DEPRECATION")
            when {
                hasBt -> {
                    audioManager.startBluetoothSco()
                    audioManager.isBluetoothScoOn = true
                }
                hasWired -> Unit // wired routes automatically in comm mode
                else -> audioManager.isSpeakerphoneOn = true
            }
        }
    }

    /** Create the local SDP offer (also sets it as the local description). */
    override suspend fun createOffer(): String {
        val offer = suspendCancellableCoroutine<SessionDescription> { cont ->
            peer.createOffer(object : SdpObserverAdapter() {
                override fun onCreateSuccess(desc: SessionDescription) { if (cont.isActive) cont.resume(desc) }
                override fun onCreateFailure(error: String?) =
                    if (cont.isActive) cont.resumeWithException(IllegalStateException("createOffer: $error")) else Unit
            }, MediaConstraints())
        }
        suspendCancellableCoroutine<Unit> { cont ->
            peer.setLocalDescription(object : SdpObserverAdapter() {
                override fun onSetSuccess() { if (cont.isActive) cont.resume(Unit) }
                override fun onSetFailure(error: String?) =
                    if (cont.isActive) cont.resumeWithException(IllegalStateException("setLocalDescription: $error")) else Unit
            }, offer)
        }
        return offer.description
    }

    /** Apply OpenAI's SDP answer; media + the data channel come up after this. */
    override suspend fun acceptAnswer(answerSdp: String) {
        suspendCancellableCoroutine<Unit> { cont ->
            peer.setRemoteDescription(object : SdpObserverAdapter() {
                override fun onSetSuccess() { if (cont.isActive) cont.resume(Unit) }
                override fun onSetFailure(error: String?) =
                    if (cont.isActive) cont.resumeWithException(IllegalStateException("setRemoteDescription: $error")) else Unit
            }, SessionDescription(SessionDescription.Type.ANSWER, answerSdp))
        }
    }

    /** Send one realtime client event (e.g. a `function_call_output` item). */
    override fun send(event: JSONObject) {
        if (closed) return
        val bytes = event.toString().toByteArray(StandardCharsets.UTF_8)
        events.send(DataChannel.Buffer(ByteBuffer.wrap(bytes), false))
    }

    /** Mute or unmute the mic track. */
    override fun setMicEnabled(enabled: Boolean) {
        micTrack.setEnabled(enabled)
    }

    override fun close() {
        if (closed) return
        closed = true
        state.value = VoiceMediaState.CLOSED
        runCatching { events.unregisterObserver() }
        runCatching { events.close() }
        runCatching { peer.close() }
        runCatching { events.dispose() }
        runCatching { peer.dispose() }
        runCatching { micTrack.dispose() }
        runCatching { audioSource.dispose() }
        runCatching { factory.dispose() }
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
            runCatching { audioManager.clearCommunicationDevice() }
        } else {
            @Suppress("DEPRECATION")
            runCatching {
                audioManager.stopBluetoothSco()
                audioManager.isBluetoothScoOn = false
                audioManager.isSpeakerphoneOn = false
            }
        }
        audioManager.mode = AudioManager.MODE_NORMAL
    }

    private inner class PeerObserver : PeerConnection.Observer {
        override fun onSignalingChange(state: PeerConnection.SignalingState?) {}
        override fun onIceConnectionChange(state: PeerConnection.IceConnectionState?) {}
        override fun onIceConnectionReceivingChange(receiving: Boolean) {}
        override fun onIceGatheringChange(state: PeerConnection.IceGatheringState?) {}
        override fun onIceCandidate(candidate: IceCandidate?) {}
        override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>?) {}
        override fun onAddStream(stream: MediaStream?) {}
        override fun onRemoveStream(stream: MediaStream?) {}
        override fun onDataChannel(channel: DataChannel?) {}
        override fun onRenegotiationNeeded() {}

        override fun onConnectionChange(newState: PeerConnection.PeerConnectionState?) {
            if (closed) return
            peerState = newState ?: PeerConnection.PeerConnectionState.NEW
            publishState()
        }
    }

    private abstract class SdpObserverAdapter : SdpObserver {
        override fun onCreateSuccess(desc: SessionDescription) {}
        override fun onSetSuccess() {}
        override fun onCreateFailure(error: String?) {}
        override fun onSetFailure(error: String?) {}
    }

    private companion object {
        const val TAG = "RealtimeClient"

        /**
         * Device types that count as "the user is already listening there".
         * Lives in the companion, NOT as an instance property: the init block
         * calls configureAudioRouting() before later-declared instance
         * properties would be initialized (that ordering bug shipped once —
         * "ArraysKt.contains, parameter <this> is null").
         */
        val EXTERNAL_TYPES = intArrayOf(
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
            AudioDeviceInfo.TYPE_BLE_HEADSET,
            AudioDeviceInfo.TYPE_WIRED_HEADSET,
            AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
            AudioDeviceInfo.TYPE_USB_HEADSET,
            AudioDeviceInfo.TYPE_USB_DEVICE,
            AudioDeviceInfo.TYPE_HEARING_AID,
        )
    }
}
