package com.hobbycoders.aishuffle.voice

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.databinding.ActivityVoiceCallBinding
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * The talk screen: a viewer over [VoiceCallState] with mute/hang-up controls.
 * The foreground service owns the call across rotation and app switching.
 * Started only for the explicitly selected server and optional existing session.
 */
class VoiceCallActivity : AppCompatActivity() {

    private lateinit var binding: ActivityVoiceCallBinding
    private var startIssued = false

    private val micPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) startCallIfIdle() else renderMicrophoneOff()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        startIssued = savedInstanceState?.getBoolean("voice_start_issued") ?: false
        if (!VoiceCallState.isActive) VoiceCallState.reset()
        binding = ActivityVoiceCallBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.muteButton.setOnClickListener { VoiceCallService.toggleMute(this) }
        binding.hangupButton.setOnClickListener {
            if (VoiceCallState.isActive) VoiceCallService.hangup(this)
            finish()
        }

        observeState()
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.RESUMED) { ensureMicThenStart() }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putBoolean("voice_start_issued", startIssued)
        super.onSaveInstanceState(outState)
    }

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        startIssued = false // A new foreground handoff is an explicit start request.
        ensureMicThenStart()
    }

    override fun onResume() {
        super.onResume()
        val id = intent.getStringExtra(VoiceCallService.EXTRA_SERVER_ID) ?: VoiceCallState.state.value.serverId
        val server = id?.let { com.hobbycoders.aishuffle.data.ServerStore.get(this).getById(it) }
        if (server != null && authorizedServerId() != null) com.hobbycoders.aishuffle.tools.DeviceToolsRuntime.attach(this, server)
        ensureMicThenStart()
    }

    override fun onPause() {
        com.hobbycoders.aishuffle.tools.DeviceToolsRuntime.detach(this)
        super.onPause()
    }

    private fun hasMicrophone() = ContextCompat.checkSelfPermission(
        this, Manifest.permission.RECORD_AUDIO,
    ) == PackageManager.PERMISSION_GRANTED

    private fun authorizedServerId(): String? = runCatching {
        val id = intent.getStringExtra(VoiceCallService.EXTRA_SERVER_ID)
            ?: VoiceCallState.state.value.serverId ?: return@runCatching null
        val server = com.hobbycoders.aishuffle.data.ServerStore.get(this).getById(id)
            ?: return@runCatching null
        if (server.apiKey.isBlank()) return@runCatching null
        if (server.accountId != null) {
            val session = com.hobbycoders.aishuffle.account.AccountStore(this).session()
            if (session == null || session.accountId != server.accountId || session.hub != server.hubUrl) return@runCatching null
        }
        id
    }.getOrNull()

    private fun renderMicrophoneOff() {
        binding.statusText.text = "Microphone is off. Voice stays off until you grant access."
        binding.muteButton.text = "Enable microphone"
        binding.orb.isActivated = false
        binding.muteButton.setOnClickListener {
            if (authorizedServerId() == null) finish()
            else micPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun ensureMicThenStart() {
        if (authorizedServerId() == null) { finish(); return }
        if (hasMicrophone()) startCallIfIdle() else renderMicrophoneOff()
    }

    private fun startCallIfIdle() {
        // A permission result or a new intent can arrive after the activity
        // paused. The RESUMED observer retries without consuming the request.
        if (!lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) return
        val serverId = authorizedServerId()
        if (serverId == null) { finish(); return }
        if (!hasMicrophone()) { renderMicrophoneOff(); return }
        binding.muteButton.setOnClickListener { VoiceCallService.toggleMute(this) }
        val sessionId = intent.getStringExtra(VoiceCallService.EXTRA_SESSION_ID)
        if (sessionId != null && !validVoiceSessionId(sessionId)) { finish(); return }
        val active = VoiceCallState.state.value
        if (VoiceCallState.isActive && (active.serverId != serverId || (sessionId != null && active.sessionId != sessionId))) {
            // A deliberate selection must never attach this screen to another account/server's call.
            android.widget.Toast.makeText(this, "End the current voice call before opening another session.", android.widget.Toast.LENGTH_LONG).show()
            finish()
            return
        }
        if (VoiceCallState.isActive) {
            startIssued = true // Reopening this active call has satisfied the foreground request.
            return
        }
        if (!VoiceCallState.isActive && !startIssued) {
            startIssued = true
            VoiceCallService.start(this, serverId, SessionMode.CHAT, sessionId)
        }
    }

    private fun observeState() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                VoiceCallState.state.collect { state ->
                    if (!hasMicrophone()) {
                        renderMicrophoneOff()
                        return@collect
                    }
                    binding.statusText.text = when (state.status) {
                        CallStatus.IDLE, CallStatus.CONNECTING ->
                            getString(R.string.voice_status_connecting)
                        CallStatus.RECONNECTING -> "Reconnecting voice chat…"
                        CallStatus.LIVE -> state.serverName
                            ?.let { getString(R.string.voice_status_live, it) }
                            ?: getString(R.string.voice_status_live_generic)
                        CallStatus.ENDED -> getString(R.string.voice_status_ended)
                        CallStatus.ERROR -> state.error
                            ?: getString(R.string.voice_status_error)
                    }
                    binding.userCaption.text = state.userCaption
                    binding.assistantCaption.text = state.assistantCaption
                    binding.muteButton.text = getString(
                        if (state.muted) R.string.voice_unmute else R.string.voice_mute,
                    )
                    binding.orb.isActivated = state.status == CallStatus.LIVE
                    if (state.status == CallStatus.ERROR) {
                        binding.muteButton.text = "Try voice again"
                        binding.muteButton.setOnClickListener { startIssued = false; ensureMicThenStart() }
                    }

                    if (state.status == CallStatus.ENDED) {
                        // Give the "Call ended" state a beat, then leave —
                        // unless a new call started while we lingered.
                        delay(1200)
                        if (VoiceCallState.state.value.status == CallStatus.ENDED) {
                            finish()
                        }
                    }
                }
            }
        }
    }
}
