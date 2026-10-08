package com.hobbycoders.aishuffle.ui

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import androidx.core.content.ContextCompat

/** Owns routing only while the authorized page has a call. PCM speaker playback
 * stays in normal/media mode; communication mode is needed only for headset SCO. */
@Suppress("DEPRECATION")
class VoiceAudioRouting(private val context: Context, private val changed: () -> Unit) {
    private val audio by lazy { context.getSystemService(Context.AUDIO_SERVICE) as AudioManager }
    private val handler = Handler(Looper.getMainLooper())
    private var active = false
    private var previousMode = AudioManager.MODE_NORMAL
    private var previousSpeaker = false
    private var previousSco = false
    private var previousDevice: AudioDeviceInfo? = null
    private var selected: Int? = -1
    private val notify = Runnable { if (active) changed() }
    private val devices = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out AudioDeviceInfo>) { update() }
        override fun onAudioDevicesRemoved(removed: Array<out AudioDeviceInfo>) { update() }
    }
    private val sco = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (active && intent?.getIntExtra(AudioManager.EXTRA_SCO_AUDIO_STATE, -1) == AudioManager.SCO_AUDIO_STATE_CONNECTED) {
                runCatching { audio.isBluetoothScoOn = true; scheduleNotification() }
                    .onFailure { android.util.Log.w("VoiceAudioRouting", "Could not finish SCO routing", it) }
            }
        }
    }

    fun start() {
        if (active) return
        runCatching {
            previousMode = audio.mode; previousSpeaker = audio.isSpeakerphoneOn; previousSco = audio.isBluetoothScoOn
            if (Build.VERSION.SDK_INT >= 31) previousDevice = audio.communicationDevice
            active = true
            audio.registerAudioDeviceCallback(devices, handler)
            if (Build.VERSION.SDK_INT < 31) ContextCompat.registerReceiver(context, sco,
                IntentFilter(AudioManager.ACTION_SCO_AUDIO_STATE_UPDATED), ContextCompat.RECEIVER_NOT_EXPORTED)
            update()
        }.onFailure {
            android.util.Log.w("VoiceAudioRouting", "Could not initialize headset routing", it)
            stop()
        }
    }

    private fun scheduleNotification() {
        // Wired/USB and Android communication routes settle asynchronously. Coalesce
        // the input/output callbacks before asking the page for a fresh default mic.
        handler.removeCallbacks(notify); handler.postDelayed(notify, 350)
    }

    private fun update() {
        if (!active) return
        runCatching {
            val available = if (Build.VERSION.SDK_INT >= 31) audio.availableCommunicationDevices
                else audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS).toList()
            val targetId = VoiceRoutePolicy.choose(available.map { VoiceRoutePolicy.Device(it.id, it.type) })
            if (selected == targetId) { scheduleNotification(); return }
            selected = targetId
            val target = available.firstOrNull { it.id == targetId }
            val bluetooth = target != null && VoiceRoutePolicy.bluetooth(target.type)
            if (bluetooth) {
                audio.mode = AudioManager.MODE_IN_COMMUNICATION
                audio.isSpeakerphoneOn = false
                if (Build.VERSION.SDK_INT >= 31) {
                    check(audio.setCommunicationDevice(target!!)) { "Headset communication route was refused" }
                } else {
                    audio.startBluetoothSco()
                }
            } else {
                if (Build.VERSION.SDK_INT >= 31) audio.clearCommunicationDevice()
                else { audio.stopBluetoothSco(); audio.isBluetoothScoOn = false }
                // No earpiece/VOICE_CALL attenuation for the streamed WebAudio voice.
                audio.mode = AudioManager.MODE_NORMAL
                audio.isSpeakerphoneOn = false
            }
        }.onFailure {
            selected = -1
            android.util.Log.w("VoiceAudioRouting", "Could not select the headset route", it)
            runCatching {
                if (Build.VERSION.SDK_INT >= 31) audio.clearCommunicationDevice()
                else { audio.stopBluetoothSco(); audio.isBluetoothScoOn = false }
                audio.mode = AudioManager.MODE_NORMAL
            }
        }
        scheduleNotification()
    }

    fun stop() {
        if (!active) return
        active = false; selected = -1
        handler.removeCallbacks(notify)
        runCatching { audio.unregisterAudioDeviceCallback(devices) }
        if (Build.VERSION.SDK_INT < 31) runCatching { context.unregisterReceiver(sco) }
        runCatching {
            if (Build.VERSION.SDK_INT >= 31) {
                audio.clearCommunicationDevice()
                previousDevice?.let { audio.setCommunicationDevice(it) }
            } else {
                audio.stopBluetoothSco(); audio.isBluetoothScoOn = previousSco
            }
            audio.mode = previousMode; audio.isSpeakerphoneOn = previousSpeaker
        }
    }
}

/** Pure device preference: wired/USB before Bluetooth, otherwise default media
 * output (speaker), not the built-in earpiece. A2DP alone has no headset mic. */
object VoiceRoutePolicy {
    data class Device(val id: Int, val type: Int)
    fun bluetooth(type: Int) = type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || type == AudioDeviceInfo.TYPE_BLE_HEADSET
    fun choose(devices: List<Device>): Int? = devices.firstOrNull {
        it.type in listOf(AudioDeviceInfo.TYPE_WIRED_HEADSET, AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
            AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_USB_DEVICE)
    }?.id ?: devices.firstOrNull { bluetooth(it.type) }?.id
}
