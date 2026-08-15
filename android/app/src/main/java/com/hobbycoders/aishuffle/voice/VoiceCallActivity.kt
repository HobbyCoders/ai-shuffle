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
 * The call itself lives in [VoiceCallService], so rotating or leaving this
 * screen never drops it. Launched by the wake word, the QS tile, the assist
 * gesture (long-press power on supported devices), or the settings screen.
 */
class VoiceCallActivity : AppCompatActivity() {

    private lateinit var binding: ActivityVoiceCallBinding

    private val micPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) startCallIfIdle() else finish()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityVoiceCallBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.muteButton.setOnClickListener { VoiceCallService.toggleMute(this) }
        binding.hangupButton.setOnClickListener {
            VoiceCallService.hangup(this)
            finish()
        }

        observeState()
        ensureMicThenStart()
    }

    private fun ensureMicThenStart() {
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.RECORD_AUDIO,
        ) == PackageManager.PERMISSION_GRANTED
        if (granted) startCallIfIdle() else micPermission.launch(Manifest.permission.RECORD_AUDIO)
    }

    private fun startCallIfIdle() {
        if (!VoiceCallState.isActive) {
            VoiceCallService.start(this)
        }
    }

    private fun observeState() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                VoiceCallState.state.collect { state ->
                    binding.statusText.text = when (state.status) {
                        CallStatus.IDLE, CallStatus.CONNECTING ->
                            getString(R.string.voice_status_connecting)
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
